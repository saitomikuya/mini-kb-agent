"""Background-job API, progress, recovery, retry, and restart tests."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
import time

from fastapi.testclient import TestClient
from huey import SqliteHuey
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import Base, build_engine
from app.jobs import JobStatus
from app.main import create_app
from app.models.job import Job, JobItem
from app.models.source_file import SourceFile
from app.services.conversion_worker import _job_heartbeat_watchdog
from app.services.jobs import (
    ItemFailure,
    ItemProcessor,
    JobService,
    fake_item_processor,
    recover_stale_jobs,
)
from app.tasks.queue import JobTaskQueue, build_job_task_queue


ADMIN_PASSWORD = "admin-background-job-tests"


@dataclass(slots=True)
class JobTestInfra:
    settings: Settings
    application: object
    client: TestClient
    queue: JobTaskQueue


@pytest.fixture
def infra_factory(tmp_path: Path) -> Iterator[Callable[..., JobTestInfra]]:
    opened_clients: list[TestClient] = []
    counter = 0

    def build(
        *,
        item_processor: ItemProcessor | None = fake_item_processor,
        retries: int = 2,
        retry_delay: int = 0,
    ) -> JobTestInfra:
        nonlocal counter
        counter += 1
        root = tmp_path / str(counter)
        settings = Settings(
            admin_password=ADMIN_PASSWORD,
            data_dir=root / "data",
            source_dir=root / "sources",
            session_max_age=3600,
            job_heartbeat_timeout=1,
        )
        settings.data_dir.mkdir(parents=True)
        engine = build_engine(settings)
        Base.metadata.create_all(engine)
        engine.dispose()

        queue = build_job_task_queue(
            settings,
            item_processor=item_processor,
            retries=retries,
            retry_delay=retry_delay,
        )
        application = create_app(settings, job_task_queue=queue)
        client = TestClient(application)
        client.__enter__()
        opened_clients.append(client)
        login = client.post(
            "/api/auth/admin/login",
            json={"password": ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        return JobTestInfra(settings, application, client, queue)

    yield build

    for client in reversed(opened_clients):
        client.__exit__(None, None, None)


def _run_next(queue: JobTaskQueue) -> None:
    task = queue.huey.dequeue()
    assert task is not None
    queue.huey.execute(task)


def test_api_returns_before_worker_then_reports_progress_and_completion(
    infra_factory,
) -> None:
    infra = infra_factory()

    response = infra.client.post("/api/admin/jobs/test-background")

    assert response.status_code == 202
    created = response.json()
    job_id = created["id"]
    assert created["status"] == "QUEUED"
    assert created["total_items"] == 5
    assert created["completed_items"] == 0
    assert len(created["items"]) == 5
    assert isinstance(infra.queue.huey, SqliteHuey)
    assert Path(infra.queue.huey.storage.filename) == infra.settings.queue_database_path
    assert infra.settings.queue_database_path.name == "queue.db"
    assert infra.queue.huey.pending_count() == 1

    assert infra.client.get("/api/admin/jobs").json()[0]["id"] == job_id
    assert infra.client.get("/api/admin/jobs/current").json()["id"] == job_id

    task = infra.queue.huey.dequeue()
    assert task is not None
    worker = Thread(target=infra.queue.huey.execute, args=(task,))
    worker.start()

    saw_partial_progress = False
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and worker.is_alive():
        current = infra.client.get(f"/api/admin/jobs/{job_id}").json()
        if current["status"] == "RUNNING" and 0 < current["completed_items"] < 5:
            saw_partial_progress = True
            break
        time.sleep(0.01)

    worker.join(timeout=3)
    assert not worker.is_alive()
    assert saw_partial_progress
    completed = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["completed_items"] == 5
    assert completed["failed_items"] == 0
    assert all(item["attempts"] == 1 for item in completed["items"])
    assert infra.client.get("/api/admin/jobs/current").json() is None


def test_delete_all_job_records_requires_no_active_jobs(infra_factory) -> None:
    infra = infra_factory()
    first = infra.client.post("/api/admin/jobs/test-background").json()

    blocked = infra.client.delete("/api/admin/jobs")

    assert blocked.status_code == 409
    assert [job["id"] for job in infra.client.get("/api/admin/jobs").json()] == [
        first["id"]
    ]

    _run_next(infra.queue)
    second = infra.client.post("/api/admin/jobs/test-background").json()
    _run_next(infra.queue)

    deleted = infra.client.delete("/api/admin/jobs")

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted_count": 2}
    assert infra.client.get("/api/admin/jobs").json() == []
    assert infra.client.get("/api/admin/jobs/current").json() is None
    with infra.application.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(JobItem)) == 0


def test_folder_conversion_queues_only_eligible_descendant_files(
    infra_factory,
) -> None:
    infra = infra_factory()
    uploads = {
        "资料包/待转换.txt": b"new",
        "资料包/产品/重试.md": b"retry",
        "资料包/已完成.txt": b"ready",
        "其他/不应转换.txt": b"outside",
    }
    records_by_path = {}
    for relative_path, content in uploads.items():
        response = infra.client.post(
            "/api/admin/files/upload",
            data={"relative_path": relative_path},
            files={"file": (Path(relative_path).name, content, "text/plain")},
        )
        assert response.status_code == 201, response.text
        records_by_path[relative_path] = response.json()

    with infra.application.state.session_factory() as session:
        retry = session.get(
            SourceFile,
            records_by_path["资料包/产品/重试.md"]["id"],
        )
        ready = session.get(
            SourceFile,
            records_by_path["资料包/已完成.txt"]["id"],
        )
        assert retry is not None and ready is not None
        retry.conversion_status = "FAILED"
        ready.conversion_status = "READY"
        session.commit()

    response = infra.client.post(
        "/api/admin/files/folder/convert",
        json={"folder_path": "资料包"},
    )

    assert response.status_code == 202, response.text
    job = response.json()
    expected_ids = {
        records_by_path["资料包/待转换.txt"]["id"],
        records_by_path["资料包/产品/重试.md"]["id"],
    }
    assert job["total_items"] == 2
    assert {item["source_file_id"] for item in job["items"]} == expected_ids
    assert infra.queue.huey.pending_count() == 1

    current = {
        record["relative_path"]: record
        for record in infra.client.get("/api/admin/files").json()
    }
    assert current["资料包/待转换.txt"]["conversion_status"] == "QUEUED"
    assert current["资料包/产品/重试.md"]["conversion_status"] == "QUEUED"
    assert current["资料包/已完成.txt"]["conversion_status"] == "READY"
    assert current["其他/不应转换.txt"]["conversion_status"] == "NEW"


def test_index_generation_job_is_enqueued_as_one_atomic_item(infra_factory) -> None:
    infra = infra_factory()

    response = infra.client.post("/api/admin/jobs/generate-index")

    assert response.status_code == 202
    job = response.json()
    assert job["job_type"] == "index_generation"
    assert job["status"] == "QUEUED"
    assert job["total_items"] == 1
    assert len(job["items"]) == 1
    assert job["items"][0]["source_file_id"] is None
    assert infra.queue.huey.pending_count() == 1


def test_default_worker_dispatch_activates_empty_index_generation(infra_factory) -> None:
    infra = infra_factory(item_processor=None)
    created = infra.client.post("/api/admin/jobs/generate-index").json()

    _run_next(infra.queue)

    detail = infra.client.get(f"/api/admin/jobs/{created['id']}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["items"][0]["progress"]["kind"] == "index"
    assert detail["items"][0]["progress"]["phase"] == "completed"
    assert detail["items"][0]["progress"]["total_documents"] == 0
    pointer = infra.settings.index_dir / "current.json"
    assert pointer.is_file()


def test_item_failure_does_not_stop_later_items(infra_factory) -> None:
    processed: list[int] = []

    def fail_second(item: JobItem, _heartbeat) -> None:
        processed.append(item.id)
        if len(processed) == 2:
            raise ItemFailure("intentional fake item failure")

    infra = infra_factory(item_processor=fail_second)
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]

    _run_next(infra.queue)

    detail = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert detail["status"] == "FAILED"
    assert detail["completed_items"] == 4
    assert detail["failed_items"] == 1
    assert len(processed) == 5
    failed_index = [item["status"] for item in detail["items"]].index("FAILED")
    assert all(
        item["status"] == "COMPLETED"
        for item in detail["items"][failed_index + 1 :]
    )


def test_job_detail_exposes_persisted_per_file_progress(infra_factory) -> None:
    infra = infra_factory()
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]
    with infra.application.state.session_factory() as session:
        item = session.scalar(
            select(JobItem).where(JobItem.job_id == job_id).order_by(JobItem.id)
        )
        assert item is not None
        item.progress_json = {
            "kind": "pdf",
            "phase": "visual_enrichment",
            "total_pages": 12,
            "direct_text_pages": 9,
            "visual_pages": 3,
            "visual_pages_completed": 1,
        }
        session.commit()

    detail = infra.client.get(f"/api/admin/jobs/{job_id}")

    assert detail.status_code == 200
    progress = detail.json()["items"][0]["progress"]
    assert progress["kind"] == "pdf"
    assert progress["total_pages"] == 12
    assert progress["visual_pages_completed"] == 1


def test_stale_recovery_is_repeatable_without_duplicate_job_or_submission(
    infra_factory,
) -> None:
    infra = infra_factory()
    with infra.application.state.session_factory() as session:
        job = Job(
            job_type="test_background",
            status=JobStatus.RUNNING,
            total_items=1,
            completed_items=0,
            failed_items=0,
            heartbeat_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            started_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        job.items = [JobItem(status=JobStatus.RUNNING, attempts=1)]
        session.add(job)
        session.commit()
        job_id = job.id

    submitted: list[int] = []
    first = recover_stale_jobs(
        infra.application.state.session_factory,
        submitted.append,
        heartbeat_timeout=1,
    )
    second = recover_stale_jobs(
        infra.application.state.session_factory,
        submitted.append,
        heartbeat_timeout=1,
    )

    assert first == [job_id]
    assert second == []
    assert submitted == [job_id]
    with infra.application.state.session_factory() as session:
        stored_job = session.get(Job, job_id)
        stored_item = session.scalar(
            select(JobItem).where(JobItem.job_id == job_id)
        )
        assert stored_job is not None and stored_job.status == "QUEUED"
        assert stored_item is not None and stored_item.status == "QUEUED"
        assert session.scalar(
            select(func.count()).select_from(Job).where(Job.id == job_id)
        ) == 1


def test_conversion_watchdog_refreshes_heartbeat_in_a_separate_process(
    infra_factory,
) -> None:
    infra = infra_factory()
    with infra.application.state.session_factory() as session:
        job = Job(
            job_type="document_conversion",
            status=JobStatus.RUNNING,
            total_items=1,
            completed_items=0,
            failed_items=0,
            heartbeat_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            started_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    with _job_heartbeat_watchdog(
        infra.settings.database_path,
        job_id,
        lambda: None,
        interval=0.03,
    ):
        time.sleep(0.12)

    with infra.application.state.session_factory() as session:
        refreshed = session.get(Job, job_id)
        assert refreshed is not None
        assert refreshed.heartbeat_at is not None
        assert refreshed.heartbeat_at.year > 2000


def test_application_startup_recovers_stale_job_once(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
        job_heartbeat_timeout=1,
    )
    settings.data_dir.mkdir()
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            Job.__table__.insert(),
            {
                "job_type": "test_background",
                "status": "RUNNING",
                "total_items": 1,
                "completed_items": 0,
                "failed_items": 0,
                "heartbeat_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                "started_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
            },
        )
        job_id = connection.execute(select(func.max(Job.id))).scalar_one()
        connection.execute(
            JobItem.__table__.insert(),
            {
                "job_id": job_id,
                "status": "RUNNING",
                "attempts": 1,
            },
        )
    engine.dispose()

    first_queue = build_job_task_queue(settings)
    first_app = create_app(settings, job_task_queue=first_queue)
    with TestClient(first_app):
        assert first_queue.huey.pending_count() == 1
        with first_app.state.session_factory() as session:
            assert session.get(Job, job_id).status == "QUEUED"

    second_queue = build_job_task_queue(settings)
    second_app = create_app(settings, job_task_queue=second_queue)
    with TestClient(second_app):
        assert second_queue.huey.pending_count() == 1
        with second_app.state.session_factory() as session:
            assert session.scalar(
                select(func.count()).select_from(Job).where(Job.id == job_id)
            ) == 1


def test_huey_retry_resumes_only_unfinished_item(infra_factory) -> None:
    calls: dict[int, int] = {}

    def fail_once(item: JobItem, _heartbeat) -> None:
        calls[item.id] = calls.get(item.id, 0) + 1
        if sum(calls.values()) == 1:
            raise RuntimeError("transient fake crash")

    infra = infra_factory(item_processor=fail_once, retries=1, retry_delay=0)
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]

    _run_next(infra.queue)
    retrying = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert retrying["status"] == "QUEUED"
    assert retrying["items"][0]["status"] == "QUEUED"
    assert retrying["items"][0]["attempts"] == 1
    assert infra.queue.huey.pending_count() == 1

    _run_next(infra.queue)
    completed = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["items"][0]["attempts"] == 2
    assert all(item["attempts"] == 1 for item in completed["items"][1:])


def test_worker_restart_leaves_running_state_for_stale_recovery(
    infra_factory,
) -> None:
    interrupted = True

    def interrupt_once(item: JobItem, _heartbeat) -> None:
        nonlocal interrupted
        if interrupted:
            interrupted = False
            raise KeyboardInterrupt

    infra = infra_factory(item_processor=interrupt_once)
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]

    _run_next(infra.queue)
    abandoned = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert abandoned["status"] == "RUNNING"
    assert abandoned["items"][0]["status"] == "RUNNING"
    assert infra.queue.huey.pending_count() == 0

    with infra.application.state.session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.heartbeat_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        session.commit()

    recovered = recover_stale_jobs(
        infra.application.state.session_factory,
        infra.queue.enqueue,
        heartbeat_timeout=1,
    )
    assert recovered == [job_id]
    _run_next(infra.queue)

    completed = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["items"][0]["attempts"] == 2


def test_duplicate_delivery_is_idempotent(infra_factory) -> None:
    infra = infra_factory(item_processor=lambda _item, _heartbeat: None)
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]
    infra.queue.enqueue(job_id)

    _run_next(infra.queue)
    _run_next(infra.queue)

    detail = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert detail["status"] == "COMPLETED"
    assert all(item["attempts"] == 1 for item in detail["items"])


def test_queued_job_can_pause_resume_and_complete(infra_factory) -> None:
    infra = infra_factory()
    created = infra.client.post("/api/admin/jobs/test-background").json()

    paused = infra.client.post(f"/api/admin/jobs/{created['id']}/pause")
    assert paused.status_code == 200
    assert paused.json()["control_state"] == "PAUSED"

    _run_next(infra.queue)
    still_paused = infra.client.get(
        f"/api/admin/jobs/{created['id']}"
    ).json()
    assert still_paused["status"] == "QUEUED"
    assert all(item["attempts"] == 0 for item in still_paused["items"])

    resumed = infra.client.post(f"/api/admin/jobs/{created['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["control_state"] == "ACTIVE"
    _run_next(infra.queue)
    assert infra.client.get(f"/api/admin/jobs/{created['id']}").json()[
        "status"
    ] == "COMPLETED"


def test_running_job_cooperatively_pauses_and_requeues_current_item(
    infra_factory,
) -> None:
    def wait_for_control(_item: JobItem, heartbeat) -> None:
        while True:
            time.sleep(0.01)
            heartbeat()

    infra = infra_factory(item_processor=wait_for_control)
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]
    task = infra.queue.huey.dequeue()
    assert task is not None
    worker = Thread(target=infra.queue.huey.execute, args=(task,))
    worker.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if infra.client.get(f"/api/admin/jobs/{job_id}").json()["status"] == "RUNNING":
            break
        time.sleep(0.01)

    response = infra.client.post(f"/api/admin/jobs/{job_id}/pause")
    assert response.status_code == 200
    worker.join(timeout=2)
    assert not worker.is_alive()
    paused = infra.client.get(f"/api/admin/jobs/{job_id}").json()
    assert paused["control_state"] == "PAUSED"
    assert paused["status"] == "QUEUED"
    assert paused["current_file_id"] is None
    assert paused["items"][0]["status"] == "QUEUED"


def test_stopped_job_can_restart_and_stopped_or_terminal_job_can_delete(
    infra_factory,
) -> None:
    infra = infra_factory()
    job_id = infra.client.post("/api/admin/jobs/test-background").json()["id"]
    assert infra.client.delete(f"/api/admin/jobs/{job_id}").status_code == 409

    stopped = infra.client.post(f"/api/admin/jobs/{job_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["control_state"] == "STOPPED"
    _run_next(infra.queue)

    restarted = infra.client.post(f"/api/admin/jobs/{job_id}/restart")
    assert restarted.status_code == 200
    assert restarted.json()["control_state"] == "ACTIVE"
    _run_next(infra.queue)
    assert infra.client.get(f"/api/admin/jobs/{job_id}").json()["status"] == "COMPLETED"

    deleted = infra.client.delete(f"/api/admin/jobs/{job_id}")
    assert deleted.status_code == 204
    assert infra.client.get(f"/api/admin/jobs/{job_id}").status_code == 404
