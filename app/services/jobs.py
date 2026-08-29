"""Durable job creation, execution, progress, retry, and recovery."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import time

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.jobs import ACTIVE_JOB_STATUSES, JobControlState, JobStatus
from app.models.job import Job, JobItem
from app.models.source_file import SourceFile
from app.schemas.job import JobDetailRead, JobRead
from app.services.source_files import (
    normalize_source_folder_path,
    source_path_in_folder,
)
from app.source_files import ConversionStatus, SourceStatus


TEST_BACKGROUND_JOB_TYPE = "test_background"
TEST_BACKGROUND_ITEM_COUNT = 5
DOCUMENT_CONVERSION_JOB_TYPE = "document_conversion"
INDEX_GENERATION_JOB_TYPE = "index_generation"
ERROR_TEXT_LIMIT = 2_000

JobEnqueuer = Callable[[int], None]
Heartbeat = Callable[[], None]
ItemProcessor = Callable[[JobItem, Heartbeat], None]


class JobServiceError(RuntimeError):
    status_code = 500


class JobNotFoundError(JobServiceError):
    status_code = 404


class JobConflictError(JobServiceError):
    status_code = 409


class ItemFailure(RuntimeError):
    """An expected per-item failure that must not abort the rest of a job."""


class JobControlSignal(BaseException):
    """Cooperative control signal that must cross conversion error wrappers."""

    def __init__(self, control_state: JobControlState) -> None:
        super().__init__(control_state.value)
        self.control_state = control_state


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fake_item_processor(_item: JobItem, heartbeat: Heartbeat) -> None:
    """Small fake workload used only to prove the background infrastructure."""
    for _ in range(2):
        time.sleep(0.05)
        heartbeat()


class JobService:
    def __init__(self, session: Session, enqueue: JobEnqueuer) -> None:
        self.session = session
        self.enqueue = enqueue

    def list_jobs(self) -> list[JobRead]:
        jobs = self.session.scalars(
            select(Job).order_by(Job.created_at.desc(), Job.id.desc())
        ).all()
        return [JobRead.model_validate(job) for job in jobs]

    def get_current_job(self) -> JobRead | None:
        job = self.session.scalar(
            select(Job)
            .where(
                Job.status.in_(ACTIVE_JOB_STATUSES),
                Job.control_state != JobControlState.STOPPED,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        return JobRead.model_validate(job) if job is not None else None

    def get_job(self, job_id: int) -> JobDetailRead:
        job = self.session.scalar(
            select(Job)
            .options(selectinload(Job.items))
            .where(Job.id == job_id)
        )
        if job is None:
            raise JobNotFoundError("Background job not found")
        return JobDetailRead.model_validate(job)

    def pause_job(self, job_id: int) -> JobDetailRead:
        job = self._get_job_model(job_id)
        if job.status not in ACTIVE_JOB_STATUSES:
            raise JobConflictError("Only an unfinished job can be paused")
        if job.control_state == JobControlState.STOPPED:
            raise JobConflictError("A stopped job must be restarted")
        job.control_state = JobControlState.PAUSED
        self.session.commit()
        return self.get_job(job_id)

    def resume_job(self, job_id: int) -> JobDetailRead:
        job = self._get_job_model(job_id)
        if job.control_state != JobControlState.PAUSED:
            raise JobConflictError("Only a paused job can be resumed")
        if job.status not in ACTIVE_JOB_STATUSES:
            raise JobConflictError("This job has already finished")
        job.control_state = JobControlState.ACTIVE
        job.finished_at = None
        job.error = None
        self.session.commit()
        self.enqueue(job.id)
        return self.get_job(job_id)

    def stop_job(self, job_id: int) -> JobDetailRead:
        job = self._get_job_model(job_id)
        if job.status not in ACTIVE_JOB_STATUSES:
            raise JobConflictError("This job has already finished")
        job.control_state = JobControlState.STOPPED
        job.finished_at = utc_now()
        self.session.commit()
        return self.get_job(job_id)

    def restart_job(self, job_id: int) -> JobDetailRead:
        job = self._get_job_model(job_id)
        if job.control_state != JobControlState.STOPPED:
            raise JobConflictError("Only a stopped job can be restarted")
        if job.status == JobStatus.RUNNING:
            raise JobConflictError("The Worker is still stopping; retry shortly")
        if not any(
            item.status in {JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING}
            for item in job.items
        ):
            raise JobConflictError("This job has no unfinished items to restart")
        job.control_state = JobControlState.ACTIVE
        job.status = JobStatus.QUEUED
        job.current_file_id = None
        job.heartbeat_at = None
        job.finished_at = None
        job.error = None
        self.session.commit()
        self.enqueue(job.id)
        return self.get_job(job_id)

    def delete_job(self, job_id: int) -> None:
        job = self._get_job_model(job_id)
        if (
            job.status in ACTIVE_JOB_STATUSES
            and job.control_state != JobControlState.STOPPED
        ):
            raise JobConflictError("Stop the job before deleting it")
        self._prepare_job_for_delete(job)
        self.session.commit()

    def delete_all_jobs(self) -> int:
        jobs = list(
            self.session.scalars(
                select(Job)
                .options(selectinload(Job.items))
                .order_by(Job.created_at.desc(), Job.id.desc())
            ).all()
        )
        if any(
            job.status in ACTIVE_JOB_STATUSES
            and job.control_state != JobControlState.STOPPED
            for job in jobs
        ):
            raise JobConflictError("请先停止所有未完成任务，再删除全部记录")
        for job in jobs:
            self._prepare_job_for_delete(job)
        self.session.commit()
        return len(jobs)

    def _prepare_job_for_delete(self, job: Job) -> None:
        unfinished_source_ids = [
            item.source_file_id
            for item in job.items
            if item.source_file_id is not None
            and item.status in {JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RUNNING}
        ]
        if unfinished_source_ids:
            self.session.execute(
                update(SourceFile)
                .where(
                    SourceFile.id.in_(unfinished_source_ids),
                    SourceFile.conversion_status.in_(
                        {ConversionStatus.QUEUED, ConversionStatus.CONVERTING}
                    ),
                )
                .values(
                    conversion_status=ConversionStatus.FAILED,
                    last_error="The background job was deleted before conversion completed",
                )
            )
        self.session.delete(job)

    def _get_job_model(self, job_id: int) -> Job:
        job = self.session.scalar(
            select(Job)
            .options(selectinload(Job.items))
            .where(Job.id == job_id)
        )
        if job is None:
            raise JobNotFoundError("Background job not found")
        return job

    def create_test_job(
        self,
        *,
        total_items: int = TEST_BACKGROUND_ITEM_COUNT,
    ) -> JobDetailRead:
        if total_items <= 0:
            raise ValueError("A test job must contain at least one item")

        job = Job(
            job_type=TEST_BACKGROUND_JOB_TYPE,
            status=JobStatus.QUEUED,
            total_items=total_items,
            completed_items=0,
            failed_items=0,
        )
        job.items = [
            JobItem(status=JobStatus.QUEUED, attempts=0)
            for _ in range(total_items)
        ]
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)

        try:
            self.enqueue(job.id)
        except Exception as exc:
            error = f"Could not enqueue background job: {_error_text(exc)}"
            job.status = JobStatus.FAILED
            job.finished_at = utc_now()
            job.error = error
            for item in job.items:
                item.status = JobStatus.FAILED
                item.finished_at = job.finished_at
                item.error = error
            job.failed_items = job.total_items
            self.session.commit()
            raise JobServiceError(error) from exc

        return self.get_job(job.id)

    def create_changed_conversion_job(
        self,
        *,
        retry_failed: bool = False,
    ) -> JobDetailRead:
        eligible_statuses = [ConversionStatus.NEW, ConversionStatus.CHANGED]
        if retry_failed:
            eligible_statuses.append(ConversionStatus.FAILED)
        records = self.session.scalars(
            select(SourceFile)
            .where(
                SourceFile.source_status == SourceStatus.PRESENT,
                SourceFile.conversion_status.in_(eligible_statuses),
            )
            .order_by(SourceFile.id)
        ).all()
        claimed: list[tuple[SourceFile, str]] = []
        for record in records:
            previous_status = record.conversion_status
            updated = self.session.execute(
                update(SourceFile)
                .where(
                    SourceFile.id == record.id,
                    SourceFile.source_status == SourceStatus.PRESENT,
                    SourceFile.conversion_status == previous_status,
                )
                .values(
                    conversion_status=ConversionStatus.QUEUED,
                    last_error=None,
                )
            )
            if updated.rowcount == 1:
                claimed.append((record, previous_status))
        return self._create_conversion_job(claimed)

    def create_reconversion_job(self) -> JobDetailRead:
        """Force every present, inactive source through conversion again."""
        records = self.session.scalars(
            select(SourceFile)
            .where(
                SourceFile.source_status == SourceStatus.PRESENT,
                SourceFile.conversion_status.not_in(
                    {ConversionStatus.QUEUED, ConversionStatus.CONVERTING}
                ),
            )
            .order_by(SourceFile.id)
        ).all()
        claimed: list[tuple[SourceFile, str]] = []
        for record in records:
            previous_status = record.conversion_status
            updated = self.session.execute(
                update(SourceFile)
                .where(
                    SourceFile.id == record.id,
                    SourceFile.source_status == SourceStatus.PRESENT,
                    SourceFile.conversion_status == previous_status,
                )
                .values(
                    conversion_status=ConversionStatus.QUEUED,
                    last_error=None,
                )
            )
            if updated.rowcount == 1:
                claimed.append((record, previous_status))
        return self._create_conversion_job(claimed)

    def create_file_conversion_job(self, file_id: int) -> JobDetailRead:
        record = self.session.get(SourceFile, file_id)
        if record is None:
            raise JobNotFoundError("Source file was not found")
        if record.source_status != SourceStatus.PRESENT:
            raise JobConflictError("Missing source files cannot be converted")
        if record.conversion_status in {
            ConversionStatus.QUEUED,
            ConversionStatus.CONVERTING,
        }:
            raise JobConflictError("Source file conversion is already active")
        previous_status = record.conversion_status
        claimed = self.session.execute(
            update(SourceFile)
            .where(
                SourceFile.id == record.id,
                SourceFile.source_status == SourceStatus.PRESENT,
                SourceFile.conversion_status == previous_status,
            )
            .values(
                conversion_status=ConversionStatus.QUEUED,
                last_error=None,
            )
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            raise JobConflictError("Source file conversion state changed; retry")
        return self._create_conversion_job([(record, previous_status)])

    def create_folder_conversion_job(
        self,
        folder_path: str,
    ) -> JobDetailRead:
        """Convert every eligible file below one logical source folder."""
        normalized_path = normalize_source_folder_path(folder_path)
        records = [
            record
            for record in self.session.scalars(
                select(SourceFile).order_by(SourceFile.id)
            ).all()
            if source_path_in_folder(record.relative_path, normalized_path)
        ]
        if not records:
            raise JobNotFoundError("Source folder was not found")

        eligible_statuses = {
            ConversionStatus.NEW,
            ConversionStatus.CHANGED,
            ConversionStatus.FAILED,
            ConversionStatus.UNSUPPORTED,
        }
        claimed: list[tuple[SourceFile, str]] = []
        for record in records:
            if (
                record.source_status != SourceStatus.PRESENT
                or record.conversion_status not in eligible_statuses
            ):
                continue
            previous_status = record.conversion_status
            updated = self.session.execute(
                update(SourceFile)
                .where(
                    SourceFile.id == record.id,
                    SourceFile.source_status == SourceStatus.PRESENT,
                    SourceFile.conversion_status == previous_status,
                )
                .values(
                    conversion_status=ConversionStatus.QUEUED,
                    last_error=None,
                )
            )
            if updated.rowcount == 1:
                claimed.append((record, previous_status))
        return self._create_conversion_job(claimed)

    def create_files_conversion_job(
        self,
        file_ids: list[int],
    ) -> JobDetailRead:
        """Create one conversion job for explicitly selected, unconverted files."""
        unique_ids = list(dict.fromkeys(file_ids))
        records_by_id = {
            record.id: record
            for record in self.session.scalars(
                select(SourceFile).where(SourceFile.id.in_(unique_ids))
            ).all()
        }
        missing_ids = [file_id for file_id in unique_ids if file_id not in records_by_id]
        if missing_ids:
            raise JobNotFoundError("One or more selected source files were not found")

        eligible_statuses = {
            ConversionStatus.NEW,
            ConversionStatus.CHANGED,
            ConversionStatus.FAILED,
            ConversionStatus.UNSUPPORTED,
        }
        records = [records_by_id[file_id] for file_id in unique_ids]
        if any(record.source_status != SourceStatus.PRESENT for record in records):
            raise JobConflictError("Missing source files cannot be converted")
        if any(record.conversion_status not in eligible_statuses for record in records):
            raise JobConflictError(
                "Only selected files that still need conversion can be converted"
            )

        claimed: list[tuple[SourceFile, str]] = []
        for record in records:
            previous_status = record.conversion_status
            updated = self.session.execute(
                update(SourceFile)
                .where(
                    SourceFile.id == record.id,
                    SourceFile.source_status == SourceStatus.PRESENT,
                    SourceFile.conversion_status == previous_status,
                )
                .values(
                    conversion_status=ConversionStatus.QUEUED,
                    last_error=None,
                )
            )
            if updated.rowcount != 1:
                self.session.rollback()
                raise JobConflictError(
                    "Source file conversion state changed; refresh and retry"
                )
            claimed.append((record, previous_status))

        return self._create_conversion_job(claimed)

    def create_index_generation_job(self) -> JobDetailRead:
        active = self.session.scalar(
            select(Job.id).where(
                Job.job_type == INDEX_GENERATION_JOB_TYPE,
                Job.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is not None:
            raise JobConflictError("Index generation is already active")

        job = Job(
            job_type=INDEX_GENERATION_JOB_TYPE,
            status=JobStatus.QUEUED,
            total_items=1,
            completed_items=0,
            failed_items=0,
        )
        job.items = [JobItem(status=JobStatus.QUEUED, attempts=0)]
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)
        try:
            self.enqueue(job.id)
        except Exception as exc:
            error = f"Could not enqueue index generation: {_error_text(exc)}"
            now = utc_now()
            job.status = JobStatus.FAILED
            job.failed_items = 1
            job.finished_at = now
            job.error = error
            job.items[0].status = JobStatus.FAILED
            job.items[0].finished_at = now
            job.items[0].error = error
            self.session.commit()
            raise JobServiceError(error) from exc
        return self.get_job(job.id)

    def _create_conversion_job(
        self,
        claimed_records: list[tuple[SourceFile, str]],
    ) -> JobDetailRead:
        records = [record for record, _ in claimed_records]
        now = utc_now()
        job = Job(
            job_type=DOCUMENT_CONVERSION_JOB_TYPE,
            status=JobStatus.QUEUED if records else JobStatus.COMPLETED,
            total_items=len(records),
            completed_items=0,
            failed_items=0,
            finished_at=None if records else now,
        )
        job.items = [
            JobItem(
                source_file_id=record.id,
                status=JobStatus.QUEUED,
                attempts=0,
            )
            for record in records
        ]
        previous_statuses = {
            record.id: previous_status
            for record, previous_status in claimed_records
        }
        self.session.add(job)
        self.session.commit()
        self.session.refresh(job)

        if not records:
            return self.get_job(job.id)

        try:
            self.enqueue(job.id)
        except Exception as exc:
            error = f"Could not enqueue document conversion: {_error_text(exc)}"
            job.status = JobStatus.FAILED
            job.finished_at = utc_now()
            job.error = error
            for item in job.items:
                item.status = JobStatus.FAILED
                item.finished_at = job.finished_at
                item.error = error
            for record in records:
                # Work never started, so preserve the actionable state that
                # selected the record instead of manufacturing a conversion
                # failure that would require an explicit retry flag.
                self.session.execute(
                    update(SourceFile)
                    .where(
                        SourceFile.id == record.id,
                        SourceFile.conversion_status == ConversionStatus.QUEUED,
                    )
                    .values(
                        conversion_status=previous_statuses[record.id],
                        last_error=error,
                    )
                )
            job.failed_items = job.total_items
            self.session.commit()
            raise JobServiceError(error) from exc

        return self.get_job(job.id)


def execute_job(
    job_id: int,
    session_factory: sessionmaker[Session],
    *,
    item_processor: ItemProcessor = fake_item_processor,
    retry_unexpected: bool = True,
    heartbeat_timeout: int = 60,
) -> None:
    """Execute or resume one job idempotently.

    Completed/failed jobs are no-ops, and a duplicate delivery cannot take a
    job that already has a fresh RUNNING lease. Only QUEUED items are claimed;
    already-completed items are never repeated.
    """
    if not _claim_job(job_id, session_factory, heartbeat_timeout):
        return

    while True:
        control_state = _check_job_control(job_id, session_factory)
        if control_state != JobControlState.ACTIVE:
            _suspend_job(job_id, None, session_factory)
            return
        item = _claim_next_item(job_id, session_factory)
        if item is None:
            control_state = _check_job_control(job_id, session_factory)
            if control_state != JobControlState.ACTIVE:
                _suspend_job(job_id, None, session_factory)
                return
            _finish_job(job_id, session_factory)
            return

        heartbeat = lambda: _heartbeat(job_id, session_factory)
        try:
            item_processor(item, heartbeat)
        except JobControlSignal:
            _suspend_job(
                job_id,
                item.id,
                session_factory,
            )
            return
        except ItemFailure as exc:
            _finish_item(
                job_id,
                item.id,
                session_factory,
                succeeded=False,
                error=_error_text(exc),
            )
        except Exception as exc:
            _handle_unexpected_failure(
                job_id,
                item.id,
                session_factory,
                exc,
                retry=retry_unexpected,
            )
            raise
        else:
            _finish_item(job_id, item.id, session_factory, succeeded=True)


def recover_stale_jobs(
    session_factory: sessionmaker[Session],
    enqueue: JobEnqueuer,
    *,
    heartbeat_timeout: int,
    now: datetime | None = None,
) -> list[int]:
    """Move stale RUNNING jobs back to QUEUED and re-submit the same ids."""
    cutoff = (now or utc_now()) - timedelta(seconds=heartbeat_timeout)
    with session_factory() as session:
        candidate_ids = session.scalars(
            select(Job.id).where(
                Job.status == JobStatus.RUNNING,
                Job.control_state == JobControlState.ACTIVE,
                or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
            )
        ).all()

    recovered: list[int] = []
    for job_id in candidate_ids:
        with session_factory() as session:
            running_source_ids = session.scalars(
                select(JobItem.source_file_id).where(
                    JobItem.job_id == job_id,
                    JobItem.status == JobStatus.RUNNING,
                    JobItem.source_file_id.is_not(None),
                )
            ).all()
            claimed = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.control_state == JobControlState.ACTIVE,
                    or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
                )
                .values(
                    status=JobStatus.QUEUED,
                    current_file_id=None,
                    heartbeat_at=None,
                    finished_at=None,
                    error=None,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                continue
            session.execute(
                update(JobItem)
                .where(
                    JobItem.job_id == job_id,
                    JobItem.status == JobStatus.RUNNING,
                )
                .values(
                    status=JobStatus.QUEUED,
                    finished_at=None,
                    error=None,
                )
            )
            if running_source_ids:
                session.execute(
                    update(SourceFile)
                    .where(
                        SourceFile.id.in_(running_source_ids),
                        SourceFile.conversion_status
                        == ConversionStatus.CONVERTING,
                    )
                    .values(conversion_status=ConversionStatus.QUEUED)
                )
            session.commit()

        enqueue(job_id)
        recovered.append(job_id)
    return recovered


def _claim_job(
    job_id: int,
    session_factory: sessionmaker[Session],
    heartbeat_timeout: int,
) -> bool:
    now = utc_now()
    cutoff = now - timedelta(seconds=heartbeat_timeout)
    with session_factory() as session:
        claimed = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.control_state == JobControlState.ACTIVE,
                or_(
                    Job.status.in_((JobStatus.PENDING, JobStatus.QUEUED)),
                    (
                        (Job.status == JobStatus.RUNNING)
                        & or_(
                            Job.heartbeat_at.is_(None),
                            Job.heartbeat_at < cutoff,
                        )
                    ),
                ),
            )
            .values(
                status=JobStatus.RUNNING,
                started_at=func.coalesce(Job.started_at, now),
                finished_at=None,
                heartbeat_at=now,
                current_file_id=None,
                error=None,
            )
        )
        if claimed.rowcount != 1:
            session.rollback()
            return False
        session.execute(
            update(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.status == JobStatus.RUNNING,
            )
            .values(status=JobStatus.QUEUED, finished_at=None, error=None)
        )
        session.commit()
        return True


def _claim_next_item(
    job_id: int,
    session_factory: sessionmaker[Session],
) -> JobItem | None:
    with session_factory() as session:
        job = session.get(Job, job_id)
        if (
            job is None
            or job.status != JobStatus.RUNNING
            or job.control_state != JobControlState.ACTIVE
        ):
            return None
        item = session.scalar(
            select(JobItem)
            .where(
                JobItem.job_id == job_id,
                JobItem.status.in_((JobStatus.PENDING, JobStatus.QUEUED)),
            )
            .order_by(JobItem.id)
            .limit(1)
        )
        if item is None:
            return None
        now = utc_now()
        item.status = JobStatus.RUNNING
        item.attempts += 1
        item.started_at = now
        item.finished_at = None
        item.error = None
        job.current_file_id = item.source_file_id
        job.heartbeat_at = now
        session.commit()
        return item


def _heartbeat(job_id: int, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise JobControlSignal(JobControlState.STOPPED)
        control_state = JobControlState(job.control_state)
        if control_state != JobControlState.ACTIVE:
            raise JobControlSignal(control_state)
        if job.status == JobStatus.RUNNING:
            job.heartbeat_at = utc_now()
        session.commit()


def _check_job_control(
    job_id: int,
    session_factory: sessionmaker[Session],
) -> JobControlState:
    with session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            return JobControlState.STOPPED
        return JobControlState(job.control_state)


def _suspend_job(
    job_id: int,
    item_id: int | None,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        persisted_control = JobControlState(job.control_state)
        effective_control = (
            persisted_control
            if persisted_control != JobControlState.ACTIVE
            else JobControlState.ACTIVE
        )
        item = session.get(JobItem, item_id) if item_id is not None else None
        if item is not None and item.status == JobStatus.RUNNING:
            item.status = JobStatus.QUEUED
            item.finished_at = None
            item.error = None
            if item.source_file_id is not None:
                session.execute(
                    update(SourceFile)
                    .where(
                        SourceFile.id == item.source_file_id,
                        SourceFile.conversion_status == ConversionStatus.CONVERTING,
                    )
                    .values(conversion_status=ConversionStatus.QUEUED)
                )
        job.control_state = effective_control
        job.status = JobStatus.QUEUED
        job.current_file_id = None
        job.heartbeat_at = None
        job.finished_at = (
            utc_now() if effective_control == JobControlState.STOPPED else None
        )
        job.error = None
        _refresh_progress(session, job)
        session.commit()


def _finish_item(
    job_id: int,
    item_id: int,
    session_factory: sessionmaker[Session],
    *,
    succeeded: bool,
    error: str | None = None,
) -> None:
    with session_factory() as session:
        item = session.get(JobItem, item_id)
        job = session.get(Job, job_id)
        if item is None or job is None or item.status != JobStatus.RUNNING:
            return
        item.status = JobStatus.COMPLETED if succeeded else JobStatus.FAILED
        item.finished_at = utc_now()
        item.error = error
        _refresh_progress(session, job)
        job.current_file_id = None
        job.heartbeat_at = utc_now()
        session.commit()


def _finish_job(job_id: int, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        _refresh_progress(session, job)
        job.status = (
            JobStatus.FAILED if job.failed_items else JobStatus.COMPLETED
        )
        job.current_file_id = None
        job.heartbeat_at = utc_now()
        job.finished_at = utc_now()
        job.error = (
            f"{job.failed_items} background item(s) failed"
            if job.failed_items
            else None
        )
        session.commit()


def _handle_unexpected_failure(
    job_id: int,
    item_id: int,
    session_factory: sessionmaker[Session],
    exc: Exception,
    *,
    retry: bool,
) -> None:
    with session_factory() as session:
        job = session.get(Job, job_id)
        item = session.get(JobItem, item_id)
        if job is None or item is None:
            return
        error = f"Unexpected background error: {_error_text(exc)}"
        now = utc_now()
        item.status = JobStatus.QUEUED if retry else JobStatus.FAILED
        item.finished_at = None if retry else now
        item.error = error
        job.status = JobStatus.QUEUED if retry else JobStatus.FAILED
        job.current_file_id = None
        job.heartbeat_at = None if retry else now
        job.finished_at = None if retry else now
        job.error = error
        _refresh_progress(session, job)
        if (
            item.source_file_id is not None
            and job.job_type == DOCUMENT_CONVERSION_JOB_TYPE
        ):
            values = (
                {
                    "conversion_status": ConversionStatus.QUEUED,
                }
                if retry
                else {
                    "conversion_status": ConversionStatus.FAILED,
                    "last_error": error,
                }
            )
            session.execute(
                update(SourceFile)
                .where(SourceFile.id == item.source_file_id)
                .values(**values)
            )
        session.commit()


def _refresh_progress(session: Session, job: Job) -> None:
    completed, failed = session.execute(
        select(
            func.count().filter(JobItem.status == JobStatus.COMPLETED),
            func.count().filter(JobItem.status == JobStatus.FAILED),
        ).where(JobItem.job_id == job.id)
    ).one()
    job.completed_items = completed
    job.failed_items = failed


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:ERROR_TEXT_LIMIT]
