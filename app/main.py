"""FastAPI application construction and system endpoints."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect

from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.llm.clients import HttpClientFactory
from app.routers.admin import api_router as admin_api_router
from app.routers.admin import router as admin_ui_router
from app.routers.auth import router as auth_router
from app.routers.chat import (
    ChatAnsweringFactory,
    ChatTitleModelFactory,
    router as chat_router,
)
from app.routers.jobs import router as jobs_router
from app.routers.model_config import router as model_config_router
from app.routers.source_files import admin_router as source_file_admin_router
from app.routers.source_files import download_router as source_file_download_router
from app.routers.tuning import router as tuning_router
from app.services.auth import AuthService
from app.services.admin import AdminArtifactServiceError
from app.services.jobs import JobServiceError, recover_stale_jobs
from app.services.model_config import ModelConfigServiceError
from app.services.secrets import APIKeyCipher
from app.services.source_files import SourceFileServiceError
from app.tasks.queue import JobTaskQueue, build_job_task_queue


def create_app(
    settings: Settings | None = None,
    *,
    model_http_client_factory: HttpClientFactory | None = None,
    job_task_queue: JobTaskQueue | None = None,
    chat_answering_service_factory: ChatAnsweringFactory | None = None,
    chat_title_model_factory: ChatTitleModelFactory | None = None,
) -> FastAPI:
    """Create the web application without starting background work."""
    active_settings = settings or get_settings()
    auth_service = AuthService(active_settings)
    database_engine = build_engine(active_settings)
    session_factory = build_session_factory(database_engine)
    api_key_cipher = APIKeyCipher(active_settings.secret_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        auth_service.ensure_secret()
        active_job_task_queue = job_task_queue
        if inspect(database_engine).has_table("jobs"):
            active_job_task_queue = active_job_task_queue or build_job_task_queue(
                active_settings,
                session_factory,
                model_http_client_factory=model_http_client_factory,
            )
            application.state.job_task_queue = active_job_task_queue
            recover_stale_jobs(
                session_factory,
                active_job_task_queue.enqueue,
                heartbeat_timeout=active_settings.job_heartbeat_timeout,
            )
        try:
            yield
        finally:
            if active_job_task_queue is not None:
                active_job_task_queue.close()
            database_engine.dispose()

    application = FastAPI(
        title=active_settings.app_name,
        version="0.7.1",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.auth_service = auth_service
    application.state.database_engine = database_engine
    application.state.session_factory = session_factory
    application.state.api_key_cipher = api_key_cipher
    application.state.model_http_client_factory = model_http_client_factory
    application.state.job_task_queue = job_task_queue
    application.state.chat_answering_service_factory = (
        chat_answering_service_factory
    )
    application.state.chat_title_model_factory = chat_title_model_factory
    application.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    application.include_router(admin_ui_router)
    application.include_router(admin_api_router)
    application.include_router(auth_router)
    application.include_router(chat_router)
    application.include_router(jobs_router)
    application.include_router(model_config_router)
    application.include_router(source_file_admin_router)
    application.include_router(source_file_download_router)
    application.include_router(tuning_router)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = []
        for raw_error in exc.errors():
            error = dict(raw_error)
            location = tuple(str(part) for part in error.get("loc", ()))
            if (
                "api_key" in location
                or "password" in location
                or "extra_headers_json" in location
            ):
                error["input"] = "********"
            errors.append(error)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": jsonable_encoder(errors)},
        )

    @application.exception_handler(ModelConfigServiceError)
    async def model_config_error_handler(
        _request: Request,
        exc: ModelConfigServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @application.exception_handler(SourceFileServiceError)
    async def source_file_error_handler(
        _request: Request,
        exc: SourceFileServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @application.exception_handler(JobServiceError)
    async def job_error_handler(
        _request: Request,
        exc: JobServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @application.exception_handler(AdminArtifactServiceError)
    async def admin_artifact_error_handler(
        _request: Request,
        exc: AdminArtifactServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
