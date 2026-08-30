"""Resolve application model roles to tested, enabled model clients."""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.llm.clients import HttpClientFactory, ModelClient, build_model_client
from app.llm.prompts import resolved_role_prompts
from app.llm.types import (
    DEFAULT_ROLE_REASONING_EFFORTS,
    ModelRole,
    ModelTestStatus,
    ReasoningEffort,
)
from app.models.model_config import (
    ModelProfile,
    ModelRoleBinding,
    ModelRolePromptSetting,
)
from app.services.secrets import APIKeyCipher


class ModelRegistryError(RuntimeError):
    """Base role-resolution error."""


class ModelRoleNotConfiguredError(ModelRegistryError):
    """Raised only when a model-dependent operation requests an unbound role."""


class ModelRoleCapabilityError(ModelRegistryError):
    """Raised when the bound model does not satisfy its role contract."""


class ModelRegistry:
    """The only business-facing route from an application role to a model."""

    def __init__(
        self,
        session: Session,
        cipher: APIKeyCipher,
        *,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.http_client_factory = http_client_factory

    def get_for_role(self, role: ModelRole) -> ModelClient:
        binding = self.session.scalar(
            select(ModelRoleBinding)
            .options(
                selectinload(ModelRoleBinding.model_profile).selectinload(
                    ModelProfile.provider
                )
            )
            .where(ModelRoleBinding.role == role.value)
        )
        if binding is None:
            raise ModelRoleNotConfiguredError(
                f"Model role '{role.value}' is not configured"
            )

        profile = binding.model_profile
        validate_profile_for_role(role, profile)
        api_key = self.cipher.decrypt(profile.provider.encrypted_api_key)
        prompt_setting = self.session.get(ModelRolePromptSetting, role.value)
        configured_effort = ReasoningEffort(
            binding.reasoning_effort or DEFAULT_ROLE_REASONING_EFFORTS[role].value
        )
        return build_model_client(
            profile.provider,
            profile,
            api_key,
            http_client_factory=self.http_client_factory,
            role_prompts=resolved_role_prompts(
                role,
                prompt_setting.prompts_json if prompt_setting is not None else None,
            ),
            role_reasoning_effort=(
                None
                if configured_effort is ReasoningEffort.MODEL_DEFAULT
                else configured_effort.value
            ),
        )


def validate_profile_for_role(role: ModelRole, profile: ModelProfile) -> None:
    if not profile.enabled or not profile.provider.enabled:
        raise ModelRoleCapabilityError(
            "The selected model profile and provider must both be enabled"
        )
    if role is ModelRole.DOCUMENT_CONVERSION:
        if not profile.supports_text or not profile.supports_vision:
            raise ModelRoleCapabilityError(
                "document_conversion requires tested text and vision support"
            )
    elif role is ModelRole.QUERY_ROUTER:
        reliable_json = profile.last_test_status in {
            ModelTestStatus.PASSED.value,
            ModelTestStatus.PARTIAL.value,
        }
        if not profile.supports_text or not reliable_json:
            raise ModelRoleCapabilityError(
                "query_router requires tested text input and reliable JSON output"
            )
    elif not profile.supports_text:
        raise ModelRoleCapabilityError(f"{role.value} requires tested text support")
