"""SqliteHuey construction shared by Web enqueuers and the Worker."""

from dataclasses import dataclass
from typing import Any

from huey import SqliteHuey
from sqlalchemy import Engine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import build_engine, build_session_factory
from app.llm.clients import HttpClientFactory
from app.models.job import Job
from app.services.conversion_worker import DocumentConversionItemProcessor
from app.services.index_worker import IndexGenerationItemProcessor
from app.services.jobs import (
    DOCUMENT_CONVERSION_JOB_TYPE,
    INDEX_GENERATION_JOB_TYPE,
    TEST_BACKGROUND_JOB_TYPE,
    ItemFailure,
    ItemProcessor,
    execute_job,
    fake_item_processor,
)
from app.services.secrets import APIKeyCipher


@dataclass(slots=True)
class JobTaskQueue:
    huey: SqliteHuey
    task: Any
    session_factory: sessionmaker[Session]
    owned_engine: Engine | None = None

    def enqueue(self, job_id: int) -> None:
        # A stable Huey id makes duplicate deliveries observable. Correctness
        # still comes from the persisted job lease and idempotent task body.
        self.task.schedule(
            args=(job_id,),
            delay=0,
            id=f"background-job-{job_id}",
        )

    def close(self) -> None:
        self.huey.storage.close()
        if self.owned_engine is not None:
            self.owned_engine.dispose()


def build_job_task_queue(
    settings: Settings,
    session_factory: sessionmaker[Session] | None = None,
    *,
    item_processor: ItemProcessor | None = None,
    model_http_client_factory: HttpClientFactory | None = None,
    retries: int = 2,
    retry_delay: int = 1,
) -> JobTaskQueue:
    """Build one SqliteHuey facade against `${DATA_DIR}/queue.db`."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    huey = SqliteHuey(
        "mini-kb-agent",
        filename=str(settings.queue_database_path),
        results=False,
        utc=True,
        strict_fifo=True,
    )

    owned_engine: Engine | None = None
    if session_factory is None:
        owned_engine = build_engine(settings)
        session_factory = build_session_factory(owned_engine)

    if item_processor is None:
        conversion_processor = DocumentConversionItemProcessor(
            settings,
            session_factory,
            APIKeyCipher(settings.secret_path),
            http_client_factory=model_http_client_factory,
        )
        index_processor = IndexGenerationItemProcessor(
            settings,
            session_factory,
            APIKeyCipher(settings.secret_path),
            http_client_factory=model_http_client_factory,
        )

        def dispatch_item(item, heartbeat) -> None:
            with session_factory() as session:
                job_type = session.scalar(
                    select(Job.job_type).where(Job.id == item.job_id)
                )
            if job_type == TEST_BACKGROUND_JOB_TYPE:
                fake_item_processor(item, heartbeat)
            elif job_type == DOCUMENT_CONVERSION_JOB_TYPE:
                conversion_processor(item, heartbeat)
            elif job_type == INDEX_GENERATION_JOB_TYPE:
                index_processor(item, heartbeat)
            else:
                raise ItemFailure(f"Unsupported background job type: {job_type}")

        item_processor = dispatch_item

    @huey.task(
        retries=retries,
        retry_delay=retry_delay,
        context=True,
        name="run_background_job",
    )
    def run_background_job(job_id: int, task: Any = None) -> None:
        execute_job(
            job_id,
            session_factory,
            item_processor=item_processor,
            retry_unexpected=bool(task is not None and task.retries),
            heartbeat_timeout=settings.job_heartbeat_timeout,
        )

    return JobTaskQueue(
        huey=huey,
        task=run_background_job,
        session_factory=session_factory,
        owned_engine=owned_engine,
    )
