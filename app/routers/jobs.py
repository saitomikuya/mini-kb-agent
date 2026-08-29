"""Admin-only background-job inspection and fake-work API."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db_session
from app.schemas.job import ConvertChangedRequest, JobDetailRead, JobRead
from app.services.jobs import JobService


router = APIRouter(
    prefix="/api/admin/jobs",
    tags=["background-jobs"],
    dependencies=[Depends(require_admin)],
)


def get_job_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JobService:
    return JobService(session, request.app.state.job_task_queue.enqueue)


ServiceDependency = Annotated[JobService, Depends(get_job_service)]


@router.get("", response_model=list[JobRead])
def list_jobs(service: ServiceDependency) -> list[JobRead]:
    return service.list_jobs()


@router.get("/current", response_model=JobRead | None)
def get_current_job(service: ServiceDependency) -> JobRead | None:
    return service.get_current_job()


@router.delete("", response_model=dict[str, int])
def delete_all_jobs(service: ServiceDependency) -> dict[str, int]:
    return {"deleted_count": service.delete_all_jobs()}


@router.post(
    "/convert-changed",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def convert_changed(
    service: ServiceDependency,
    payload: ConvertChangedRequest | None = Body(default=None),
    retry_failed: bool = Query(default=False),
    retry: bool = Query(default=False),
) -> JobDetailRead:
    explicit_retry = retry_failed or retry or bool(payload and payload.retry)
    return service.create_changed_conversion_job(retry_failed=explicit_retry)


@router.post(
    "/reconvert-all",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def reconvert_all(service: ServiceDependency) -> JobDetailRead:
    return service.create_reconversion_job()


@router.post(
    "/generate-index",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_index(service: ServiceDependency) -> JobDetailRead:
    return service.create_index_generation_job()


@router.get("/{id}", response_model=JobDetailRead)
def get_job(id: int, service: ServiceDependency) -> JobDetailRead:
    return service.get_job(id)


@router.post("/{id}/pause", response_model=JobDetailRead)
def pause_job(id: int, service: ServiceDependency) -> JobDetailRead:
    return service.pause_job(id)


@router.post("/{id}/resume", response_model=JobDetailRead)
def resume_job(id: int, service: ServiceDependency) -> JobDetailRead:
    return service.resume_job(id)


@router.post("/{id}/stop", response_model=JobDetailRead)
def stop_job(id: int, service: ServiceDependency) -> JobDetailRead:
    return service.stop_job(id)


@router.post("/{id}/restart", response_model=JobDetailRead)
def restart_job(id: int, service: ServiceDependency) -> JobDetailRead:
    return service.restart_job(id)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(id: int, service: ServiceDependency) -> Response:
    service.delete_job(id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/test-background",
    response_model=JobDetailRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_test_background_job(service: ServiceDependency) -> JobDetailRead:
    return service.create_test_job()
