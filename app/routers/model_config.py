"""Admin-only Provider, Model Profile, test, and role-binding APIs."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db_session
from app.llm.types import ModelRole
from app.schemas.model_config import (
    APIProviderCreate,
    APIProviderRead,
    APIProviderUpdate,
    ModelProfileCreate,
    ModelProfileRead,
    ModelProfileTestRead,
    ModelProfileUpdate,
    ModelRoleBindingRead,
    ModelRoleBindingUpdate,
    ModelRolePromptUpdate,
)
from app.services.model_config import ModelConfigService


router = APIRouter(
    prefix="/api/admin",
    tags=["model-configuration"],
    dependencies=[Depends(require_admin)],
)


def get_model_config_service(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> ModelConfigService:
    return ModelConfigService(
        session,
        request.app.state.api_key_cipher,
        http_client_factory=request.app.state.model_http_client_factory,
    )


ServiceDependency = Annotated[ModelConfigService, Depends(get_model_config_service)]


@router.get("/providers", response_model=list[APIProviderRead])
def list_providers(service: ServiceDependency) -> list[APIProviderRead]:
    return service.list_providers()


@router.post(
    "/providers",
    response_model=APIProviderRead,
    status_code=status.HTTP_201_CREATED,
)
def create_provider(
    data: APIProviderCreate,
    service: ServiceDependency,
) -> APIProviderRead:
    return service.create_provider(data)


@router.get("/providers/{provider_id}", response_model=APIProviderRead)
def get_provider(provider_id: int, service: ServiceDependency) -> APIProviderRead:
    return service.get_provider(provider_id)


@router.put("/providers/{provider_id}", response_model=APIProviderRead)
def update_provider(
    provider_id: int,
    data: APIProviderUpdate,
    service: ServiceDependency,
) -> APIProviderRead:
    return service.update_provider(provider_id, data)


@router.delete(
    "/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_provider(provider_id: int, service: ServiceDependency) -> Response:
    service.delete_provider(provider_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/model-profiles", response_model=list[ModelProfileRead])
def list_profiles(
    service: ServiceDependency,
    provider_id: Annotated[int | None, Query()] = None,
) -> list[ModelProfileRead]:
    return service.list_profiles(provider_id)


@router.post(
    "/model-profiles",
    response_model=ModelProfileRead,
    status_code=status.HTTP_201_CREATED,
)
def create_profile(
    data: ModelProfileCreate,
    service: ServiceDependency,
) -> ModelProfileRead:
    return service.create_profile(data)


@router.get("/model-profiles/{profile_id}", response_model=ModelProfileRead)
def get_profile(profile_id: int, service: ServiceDependency) -> ModelProfileRead:
    return service.get_profile(profile_id)


@router.put("/model-profiles/{profile_id}", response_model=ModelProfileRead)
def update_profile(
    profile_id: int,
    data: ModelProfileUpdate,
    service: ServiceDependency,
) -> ModelProfileRead:
    return service.update_profile(profile_id, data)


@router.delete(
    "/model-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_profile(profile_id: int, service: ServiceDependency) -> Response:
    service.delete_profile(profile_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/model-profiles/{profile_id}/test",
    response_model=ModelProfileTestRead,
)
async def test_profile(
    profile_id: int,
    service: ServiceDependency,
) -> ModelProfileTestRead:
    return await service.test_profile(profile_id)


@router.get("/model-roles", response_model=list[ModelRoleBindingRead])
def list_model_roles(service: ServiceDependency) -> list[ModelRoleBindingRead]:
    return service.list_role_bindings()


@router.put("/model-roles/{role}", response_model=ModelRoleBindingRead)
def bind_model_role(
    role: ModelRole,
    data: ModelRoleBindingUpdate,
    service: ServiceDependency,
) -> ModelRoleBindingRead:
    return service.bind_role(role, data.model_profile_id, data.reasoning_effort)


@router.put("/model-roles/{role}/prompts", response_model=ModelRoleBindingRead)
def update_model_role_prompts(
    role: ModelRole,
    data: ModelRolePromptUpdate,
    service: ServiceDependency,
) -> ModelRoleBindingRead:
    return service.update_role_prompts(role, data.prompts)
