"""Worker orchestration for one persisted document-conversion job item."""

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.llm.clients import HttpClientFactory
from app.llm.registry import ModelRegistry
from app.llm.types import ModelRole
from app.models.job import JobItem
from app.models.source_file import SourceFile
from app.services.document_conversion import (
    DocumentConversionEngine,
    DocumentConversionError,
    SourceDocument,
    UnsupportedDocumentError,
)
from app.services.jobs import Heartbeat, ItemFailure
from app.services.secrets import APIKeyCipher
from app.services.source_files import safe_source_path, sha256_file
from app.services.tuning import effective_settings
from app.source_files import ConversionStatus, IndexStatus, SourceStatus


logger = logging.getLogger(__name__)


class DocumentConversionItemProcessor:
    """Run conversion for one item and persist its source-file state."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        cipher: APIKeyCipher,
        *,
        http_client_factory: HttpClientFactory | None = None,
        engine_factory: Callable[..., DocumentConversionEngine] = DocumentConversionEngine,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cipher = cipher
        self.http_client_factory = http_client_factory
        self.engine_factory = engine_factory

    def __call__(self, item: JobItem, heartbeat: Heartbeat) -> None:
        if item.source_file_id is None:
            raise ItemFailure("Document-conversion item has no source file")

        logger.info(
            "Conversion job %s item %s: loading source record",
            item.job_id,
            item.id,
        )
        with self.session_factory() as session:
            record = session.get(SourceFile, item.source_file_id)
            if record is None:
                raise ItemFailure("Source file record no longer exists")
            if record.source_status != SourceStatus.PRESENT:
                self._mark_failed(session, record, "Source file is missing")
                raise ItemFailure("Source file is missing")

            path = safe_source_path(self.settings.source_dir, record.relative_path)
            if not path.is_file():
                record.source_status = SourceStatus.MISSING
                self._mark_failed(session, record, "Source file is missing")
                raise ItemFailure("Source file is missing")

            record.conversion_status = ConversionStatus.CONVERTING
            record.last_error = None
            persisted_item = session.get(JobItem, item.id)
            if persisted_item is not None:
                persisted_item.progress_json = {
                    "kind": path.suffix.lower().removeprefix(".") or "unknown",
                    "phase": "preparing",
                }
            session.commit()
            logger.info(
                "Conversion job %s item %s: source claimed; verifying hash",
                item.job_id,
                item.id,
            )

            try:
                actual_hash = sha256_file(path)
                if actual_hash != record.sha256:
                    raise DocumentConversionError(
                        "Source content changed after inventory scan; scan and retry"
                    )
                logger.info(
                    "Conversion job %s item %s: hash verified; preparing engine",
                    item.job_id,
                    item.id,
                )

                registry = ModelRegistry(
                    session,
                    self.cipher,
                    http_client_factory=self.http_client_factory,
                )

                def resolve_model(role: ModelRole):
                    if role is not ModelRole.DOCUMENT_CONVERSION:
                        raise RuntimeError(
                            "Document conversion attempted to resolve the wrong model role"
                        )
                    return registry.get_for_role(ModelRole.DOCUMENT_CONVERSION)

                active_settings = effective_settings(session, self.settings)
                engine = self.engine_factory(
                    active_settings,
                    model_resolver=resolve_model,
                    rows_per_part=active_settings.document_excel_rows_per_part,
                    text_chars_per_part=active_settings.document_text_chars_per_part,
                )
                source = SourceDocument(
                    document_id=record.id,
                    source_path=record.relative_path,
                    source_sha256=record.sha256,
                    path=path,
                )

                def report_progress(progress: dict[str, object]) -> None:
                    """Persist progress without allowing UI telemetry to fail work."""
                    try:
                        progress_item = session.get(JobItem, item.id)
                        if progress_item is None:
                            return
                        merged = dict(progress_item.progress_json or {})
                        merged.update(progress)
                        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
                        progress_item.progress_json = merged
                        session.commit()
                    except SQLAlchemyError:
                        session.rollback()
                        logger.exception(
                            "Conversion job %s item %s: progress persistence failed",
                            item.job_id,
                            item.id,
                        )
                logger.info(
                    "Conversion job %s item %s: starting conversion watchdog",
                    item.job_id,
                    item.id,
                )
                with _job_heartbeat_watchdog(
                    self.settings.database_path,
                    item.job_id,
                    heartbeat,
                ):
                    logger.info(
                        "Conversion job %s item %s: entering document engine",
                        item.job_id,
                        item.id,
                    )
                    staged = engine.stage(
                        source,
                        job_id=item.job_id,
                        heartbeat=heartbeat,
                        progress=report_progress,
                    )

                # Source management may replace a file while a Worker is busy.
                # Never publish output unless the exact inventoried bytes remain.
                session.refresh(record)
                if (
                    record.source_status != SourceStatus.PRESENT
                    or record.sha256 != source.source_sha256
                    or sha256_file(path) != source.source_sha256
                ):
                    engine.discard(staged)
                    raise DocumentConversionError(
                        "Source content changed during conversion; retry the new version"
                    )

                engine.publish(staged)
                report_progress({"phase": "completed"})
            except UnsupportedDocumentError as exc:
                record.conversion_status = ConversionStatus.UNSUPPORTED
                record.last_error = str(exc)
                session.commit()
                raise ItemFailure(str(exc)) from exc
            except DocumentConversionError as exc:
                self._mark_failed(session, record, str(exc))
                raise ItemFailure(str(exc)) from exc

            record.conversion_status = ConversionStatus.READY
            record.index_status = _stale_if_indexed(record.index_status)
            record.last_error = None
            record.converted_at = staged.converted_at
            session.commit()

    @staticmethod
    def _mark_failed(session: Session, record: SourceFile, error: str) -> None:
        record.conversion_status = ConversionStatus.FAILED
        record.last_error = error[:2_000]
        session.commit()


def _stale_if_indexed(index_status: str) -> str:
    if index_status == IndexStatus.INDEXED:
        return IndexStatus.STALE
    return index_status


@contextmanager
def _job_heartbeat_watchdog(
    database_path: Path,
    job_id: int,
    heartbeat: Heartbeat,
    *,
    interval: float = 15.0,
) -> Iterator[None]:
    """Keep a conversion lease fresh outside the Worker's Python process."""
    heartbeat()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.services.job_heartbeat_watchdog",
            "--database",
            str(database_path.resolve()),
            "--job-id",
            str(job_id),
            "--parent-pid",
            str(os.getpid()),
            "--interval",
            str(interval),
        ],
    )
    logger.info(
        "Conversion job %s: heartbeat watchdog started with PID %s",
        job_id,
        process.pid,
    )
    try:
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        logger.info(
            "Conversion job %s: heartbeat watchdog stopped",
            job_id,
        )
