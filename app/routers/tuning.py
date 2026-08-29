"""Admin-only retrieval, token, index, and answer tuning APIs."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db_session
from app.schemas.tuning import KnowledgeTuningRead, KnowledgeTuningValues
from app.services.tuning import KnowledgeTuningService


router = APIRouter(
    prefix="/api/admin/tuning",
    tags=["knowledge-tuning"],
    dependencies=[Depends(require_admin)],
)


def get_tuning_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> KnowledgeTuningService:
    return KnowledgeTuningService(session, request.app.state.settings)


ServiceDependency = Annotated[
    KnowledgeTuningService,
    Depends(get_tuning_service),
]


@router.get("", response_model=KnowledgeTuningRead)
def get_tuning(service: ServiceDependency) -> KnowledgeTuningRead:
    return service.get()


@router.put("", response_model=KnowledgeTuningRead)
def update_tuning(
    values: KnowledgeTuningValues,
    service: ServiceDependency,
) -> KnowledgeTuningRead:
    return service.update(values)


@router.delete("", response_model=KnowledgeTuningRead)
def reset_tuning(service: ServiceDependency) -> KnowledgeTuningRead:
    return service.reset()
