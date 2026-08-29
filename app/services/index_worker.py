"""Worker orchestration for one complete hierarchical-index generation."""

from collections.abc import Callable
from datetime import datetime, timezone
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.llm.clients import HttpClientFactory
from app.llm.registry import ModelRegistry
from app.llm.types import ModelRole
from app.models.job import Job, JobItem
from app.services.index_generation import IndexGenerationError, IndexGenerationService
from app.services.jobs import Heartbeat, ItemFailure
from app.services.secrets import APIKeyCipher
from app.services.tuning import effective_settings


logger = logging.getLogger(__name__)


class IndexGenerationItemProcessor:
    """Build and activate one generation as one durable background-job item."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        cipher: APIKeyCipher,
        *,
        http_client_factory: HttpClientFactory | None = None,
        service_factory: Callable[..., IndexGenerationService] = IndexGenerationService,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cipher = cipher
        self.http_client_factory = http_client_factory
        self.service_factory = service_factory

    def __call__(self, item: JobItem, heartbeat: Heartbeat) -> None:
        def report_progress(progress: dict[str, object]) -> None:
            try:
                with self.session_factory() as progress_session:
                    progress_item = progress_session.get(JobItem, item.id)
                    job = progress_session.get(Job, item.job_id)
                    if progress_item is None or job is None:
                        return
                    merged = dict(progress_item.progress_json or {})
                    merged.update(progress)
                    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
                    progress_item.progress_json = merged
                    if "current_document_id" in progress:
                        current_id = progress.get("current_document_id")
                        job.current_file_id = (
                            int(current_id) if current_id is not None else None
                        )
                    progress_session.commit()
            except SQLAlchemyError:
                logger.exception(
                    "Index job %s item %s: progress persistence failed",
                    item.job_id,
                    item.id,
                )

        report_progress({"kind": "index", "phase": "preparing"})
        with self.session_factory() as session:
            registry = ModelRegistry(
                session,
                self.cipher,
                http_client_factory=self.http_client_factory,
            )

            def resolve_model(role: ModelRole):
                if role is not ModelRole.INDEX_GENERATION:
                    raise RuntimeError(
                        "Index generation attempted to resolve the wrong model role"
                    )
                return registry.get_for_role(ModelRole.INDEX_GENERATION)

            service = self.service_factory(
                effective_settings(session, self.settings),
                session,
                model_resolver=resolve_model,
            )
            try:
                service.build_and_activate(
                    heartbeat=heartbeat,
                    progress=report_progress,
                )
            except IndexGenerationError as exc:
                report_progress({"phase": "failed", "error": str(exc)})
                raise ItemFailure(str(exc)) from exc
