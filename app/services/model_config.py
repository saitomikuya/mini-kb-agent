"""Model configuration orchestration and role-gated client lookup."""

from datetime import datetime, timezone
from pathlib import Path
import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.llm.clients import (
    HttpClientFactory,
    JSONGeneration,
    ModelClient,
    ModelClientError,
    TextGeneration,
    build_model_client,
)
from app.llm.prompts import (
    prompt_task_definitions,
    resolved_role_prompts,
    validate_role_prompts,
)
from app.llm.registry import (
    ModelRoleCapabilityError,
    validate_profile_for_role,
)
from app.llm.types import ALL_MODEL_ROLES, ModelRole, ModelTestStatus, TestedProtocol
from app.models.model_config import (
    APIProvider,
    ModelProfile,
    ModelRoleBinding,
    ModelRolePromptSetting,
)
from app.schemas.model_config import (
    APIProviderCreate,
    APIProviderRead,
    APIProviderUpdate,
    ModelProfileCreate,
    ModelProfileRead,
    ModelProfileTestRead,
    ModelProfileUpdate,
    ModelRoleBindingRead,
    ModelRolePromptTaskRead,
    ModelTestProbeRead,
)
from app.services.secrets import APIKeyCipher, mask_api_key


class ModelConfigServiceError(RuntimeError):
    status_code = 400


class ModelConfigNotFoundError(ModelConfigServiceError):
    status_code = 404


class ModelConfigConflictError(ModelConfigServiceError):
    status_code = 409


class ModelCapabilityError(ModelConfigServiceError):
    status_code = 422


class ModelPromptValidationError(ModelConfigServiceError):
    status_code = 422


_REQUEST_AFFECTING_PROVIDER_FIELDS = {
    "provider_type",
    "base_url",
    "api_key",
    "protocol_preference",
    "extra_headers_json",
    "azure_mode",
    "azure_api_version",
}
_REQUEST_AFFECTING_PROFILE_FIELDS = {
    "provider_id",
    "remote_model_name",
    "protocol_override",
    "max_output_tokens",
    "reasoning_effort",
    "extra_request_json",
}


class ModelConfigService:
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

    def list_providers(self) -> list[APIProviderRead]:
        providers = self.session.scalars(
            select(APIProvider).order_by(APIProvider.id)
        ).all()
        return [self._provider_read(provider) for provider in providers]

    def get_provider(self, provider_id: int) -> APIProviderRead:
        return self._provider_read(self._require_provider(provider_id))

    def create_provider(self, data: APIProviderCreate) -> APIProviderRead:
        provider = APIProvider(
            name=data.name,
            provider_type=data.provider_type.value,
            base_url=data.base_url.rstrip("/"),
            encrypted_api_key=self.cipher.encrypt(data.api_key.get_secret_value()),
            protocol_preference=data.protocol_preference.value,
            extra_headers_json=data.extra_headers_json,
            azure_mode=data.azure_mode.value,
            azure_api_version=data.azure_api_version,
            enabled=data.enabled,
        )
        self.session.add(provider)
        self._commit_conflict("A provider with this name already exists")
        self.session.refresh(provider)
        return self._provider_read(provider)

    def update_provider(
        self,
        provider_id: int,
        data: APIProviderUpdate,
    ) -> APIProviderRead:
        provider = self._require_provider(provider_id)
        fields_set = data.model_fields_set
        for field_name in fields_set:
            value = getattr(data, field_name)
            if field_name == "api_key":
                if value is not None:
                    provider.encrypted_api_key = self.cipher.encrypt(
                        value.get_secret_value()
                    )
            elif field_name in {"provider_type", "protocol_preference", "azure_mode"}:
                if value is not None:
                    setattr(provider, field_name, value.value)
            elif field_name == "base_url":
                if value is not None:
                    provider.base_url = value.rstrip("/")
            elif field_name == "azure_api_version":
                provider.azure_api_version = value
            elif value is not None:
                setattr(provider, field_name, value)

        if fields_set & _REQUEST_AFFECTING_PROVIDER_FIELDS:
            self._reset_provider_test_results(provider.id)
        self._commit_conflict("A provider with this name already exists")
        self.session.refresh(provider)
        return self._provider_read(provider)

    def delete_provider(self, provider_id: int) -> None:
        provider = self._require_provider(provider_id)
        profile_count = self.session.scalar(
            select(func.count(ModelProfile.id)).where(
                ModelProfile.provider_id == provider_id
            )
        )
        if profile_count:
            raise ModelConfigConflictError(
                "Provider cannot be deleted while it owns model profiles"
            )
        self.session.delete(provider)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ModelConfigConflictError(
                "Provider cannot be deleted while it owns model profiles"
            ) from exc

    def list_profiles(self, provider_id: int | None = None) -> list[ModelProfileRead]:
        statement = select(ModelProfile).order_by(ModelProfile.id)
        if provider_id is not None:
            statement = statement.where(ModelProfile.provider_id == provider_id)
        return [
            ModelProfileRead.model_validate(profile)
            for profile in self.session.scalars(statement).all()
        ]

    def get_profile(self, profile_id: int) -> ModelProfileRead:
        return ModelProfileRead.model_validate(self._require_profile(profile_id))

    def create_profile(self, data: ModelProfileCreate) -> ModelProfileRead:
        self._require_provider(data.provider_id)
        profile = ModelProfile(
            provider_id=data.provider_id,
            name=data.name,
            remote_model_name=data.remote_model_name,
            protocol_override=(
                data.protocol_override.value if data.protocol_override is not None else None
            ),
            context_window=data.context_window,
            max_output_tokens=data.max_output_tokens,
            reasoning_effort=data.reasoning_effort,
            extra_request_json=data.extra_request_json,
            enabled=data.enabled,
        )
        self.session.add(profile)
        self._commit_conflict(
            "A model profile with this name already exists for the provider"
        )
        self.session.refresh(profile)
        return ModelProfileRead.model_validate(profile)

    def update_profile(
        self,
        profile_id: int,
        data: ModelProfileUpdate,
    ) -> ModelProfileRead:
        profile = self._require_profile(profile_id)
        fields_set = data.model_fields_set
        if "provider_id" in fields_set and data.provider_id is not None:
            self._require_provider(data.provider_id)

        for field_name in fields_set:
            value = getattr(data, field_name)
            if field_name == "protocol_override":
                profile.protocol_override = value.value if value is not None else None
            elif field_name in {
                "context_window",
                "max_output_tokens",
                "reasoning_effort",
            }:
                setattr(profile, field_name, value)
            elif value is not None:
                setattr(profile, field_name, value)

        if fields_set & _REQUEST_AFFECTING_PROFILE_FIELDS:
            _reset_profile_test_result(profile)
        self._commit_conflict(
            "A model profile with this name already exists for the provider"
        )
        self.session.refresh(profile)
        return ModelProfileRead.model_validate(profile)

    def delete_profile(self, profile_id: int) -> None:
        profile = self._require_profile(profile_id)
        binding_count = self.session.scalar(
            select(func.count(ModelRoleBinding.role)).where(
                ModelRoleBinding.model_profile_id == profile_id
            )
        )
        if binding_count:
            raise ModelConfigConflictError(
                "Model profile cannot be deleted while a model role references it"
            )
        self.session.delete(profile)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ModelConfigConflictError(
                "Model profile cannot be deleted while a model role references it"
            ) from exc

    def list_role_bindings(self) -> list[ModelRoleBindingRead]:
        bindings = {
            ModelRole(binding.role): binding
            for binding in self.session.scalars(select(ModelRoleBinding)).all()
        }
        prompt_settings = {
            ModelRole(setting.role): setting
            for setting in self.session.scalars(
                select(ModelRolePromptSetting)
            ).all()
        }
        return [
            self._role_binding_read(
                role,
                bindings.get(role),
                prompt_settings.get(role),
            )
            for role in ALL_MODEL_ROLES
        ]

    def bind_role(
        self,
        role: ModelRole,
        model_profile_id: int | None,
    ) -> ModelRoleBindingRead:
        existing = self.session.get(ModelRoleBinding, role.value)
        if model_profile_id is None:
            if existing is not None:
                self.session.delete(existing)
                self.session.commit()
            return ModelRoleBindingRead(
                role=role,
                model_profile_id=None,
                updated_at=None,
                prompts_updated_at=self._prompt_updated_at(role),
                prompt_tasks=self._prompt_task_reads(role),
            )

        profile = self._require_profile_with_provider(model_profile_id)
        try:
            validate_profile_for_role(role, profile)
        except ModelRoleCapabilityError as exc:
            raise ModelCapabilityError(str(exc)) from exc
        now = datetime.now(timezone.utc)
        if existing is None:
            existing = ModelRoleBinding(
                role=role.value,
                model_profile_id=model_profile_id,
                updated_at=now,
            )
            self.session.add(existing)
        else:
            existing.model_profile_id = model_profile_id
            existing.updated_at = now
        self.session.commit()
        self.session.refresh(existing)
        return self._role_binding_read(
            role,
            existing,
            self.session.get(ModelRolePromptSetting, role.value),
        )

    def update_role_prompts(
        self,
        role: ModelRole,
        prompts: dict[str, str],
    ) -> ModelRoleBindingRead:
        try:
            validated = validate_role_prompts(role, prompts)
        except ValueError as exc:
            raise ModelPromptValidationError(str(exc)) from exc

        setting = self.session.get(ModelRolePromptSetting, role.value)
        now = datetime.now(timezone.utc)
        if setting is None:
            setting = ModelRolePromptSetting(
                role=role.value,
                prompts_json=validated,
                updated_at=now,
            )
            self.session.add(setting)
        else:
            setting.prompts_json = validated
            setting.updated_at = now
        self.session.commit()
        self.session.refresh(setting)
        return self._role_binding_read(
            role,
            self.session.get(ModelRoleBinding, role.value),
            setting,
        )

    async def test_profile(self, profile_id: int) -> ModelProfileTestRead:
        profile = self._require_profile_with_provider(profile_id)
        api_key = self.cipher.decrypt(profile.provider.encrypted_api_key)
        client = build_model_client(
            profile.provider,
            profile,
            api_key,
            http_client_factory=self.http_client_factory,
        )
        overall_started = time.perf_counter()

        text_result, text_probe = await _run_text_probe(client)
        json_result, json_probe = await _run_json_probe(client)
        vision_result, vision_probe = await _run_vision_probe(client)

        supports_text = text_probe.passed
        json_is_reliable = json_probe.passed
        supports_vision = vision_probe.passed
        supports_structured_output = bool(
            json_probe.passed and json_probe.native_structured_output
        )
        if supports_text and json_is_reliable and supports_vision:
            status = ModelTestStatus.PASSED
        elif supports_text and json_is_reliable:
            status = ModelTestStatus.PARTIAL
        else:
            status = ModelTestStatus.FAILED

        tested_protocol = _tested_protocol_from_results(
            text_result,
            json_result,
            vision_result,
        )
        total_latency_ms = max(
            0,
            round((time.perf_counter() - overall_started) * 1000),
        )
        profile.supports_text = supports_text
        profile.supports_vision = supports_vision
        profile.supports_structured_output = supports_structured_output
        profile.tested_protocol = (
            tested_protocol.value if tested_protocol is not None else None
        )
        profile.last_test_status = status.value
        profile.last_test_latency_ms = total_latency_ms
        profile.last_tested_at = datetime.now(timezone.utc)
        self.session.commit()

        return ModelProfileTestRead(
            model_profile_id=profile.id,
            status=status,
            tested_protocol=tested_protocol,
            latency_ms=total_latency_ms,
            supports_text=supports_text,
            supports_vision=supports_vision,
            supports_structured_output=supports_structured_output,
            text=text_probe,
            json_probe=json_probe,
            vision=vision_probe,
        )

    def _provider_read(self, provider: APIProvider) -> APIProviderRead:
        return APIProviderRead(
            id=provider.id,
            name=provider.name,
            provider_type=provider.provider_type,
            base_url=provider.base_url,
            api_key_masked=mask_api_key(
                self.cipher.decrypt(provider.encrypted_api_key)
            ),
            protocol_preference=provider.protocol_preference,
            extra_headers_json=provider.extra_headers_json,
            azure_mode=provider.azure_mode,
            azure_api_version=provider.azure_api_version,
            enabled=provider.enabled,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )

    def _role_binding_read(
        self,
        role: ModelRole,
        binding: ModelRoleBinding | None,
        prompt_setting: ModelRolePromptSetting | None,
    ) -> ModelRoleBindingRead:
        return ModelRoleBindingRead(
            role=role,
            model_profile_id=(binding.model_profile_id if binding is not None else None),
            updated_at=binding.updated_at if binding is not None else None,
            prompts_updated_at=(
                prompt_setting.updated_at if prompt_setting is not None else None
            ),
            prompt_tasks=self._prompt_task_reads(role, prompt_setting),
        )

    def _prompt_task_reads(
        self,
        role: ModelRole,
        prompt_setting: ModelRolePromptSetting | None = None,
    ) -> list[ModelRolePromptTaskRead]:
        setting = prompt_setting or self.session.get(
            ModelRolePromptSetting,
            role.value,
        )
        prompts = resolved_role_prompts(
            role,
            setting.prompts_json if setting is not None else None,
        )
        return [
            ModelRolePromptTaskRead(
                task=definition.task,
                name=definition.name,
                description=definition.description,
                prompt=prompts[definition.task],
                default_prompt=definition.default_prompt,
            )
            for definition in prompt_task_definitions(role)
        ]

    def _prompt_updated_at(self, role: ModelRole) -> datetime | None:
        setting = self.session.get(ModelRolePromptSetting, role.value)
        return setting.updated_at if setting is not None else None

    def _require_provider(self, provider_id: int) -> APIProvider:
        provider = self.session.get(APIProvider, provider_id)
        if provider is None:
            raise ModelConfigNotFoundError("API provider was not found")
        return provider

    def _require_profile(self, profile_id: int) -> ModelProfile:
        profile = self.session.get(ModelProfile, profile_id)
        if profile is None:
            raise ModelConfigNotFoundError("Model profile was not found")
        return profile

    def _require_profile_with_provider(self, profile_id: int) -> ModelProfile:
        profile = self.session.scalar(
            select(ModelProfile)
            .options(selectinload(ModelProfile.provider))
            .where(ModelProfile.id == profile_id)
        )
        if profile is None:
            raise ModelConfigNotFoundError("Model profile was not found")
        return profile

    def _reset_provider_test_results(self, provider_id: int) -> None:
        profiles = self.session.scalars(
            select(ModelProfile).where(ModelProfile.provider_id == provider_id)
        ).all()
        for profile in profiles:
            _reset_profile_test_result(profile)

    def _commit_conflict(self, message: str) -> None:
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ModelConfigConflictError(message) from exc


def _reset_profile_test_result(profile: ModelProfile) -> None:
    profile.supports_text = False
    profile.supports_vision = False
    profile.supports_structured_output = False
    profile.tested_protocol = None
    profile.last_test_status = None
    profile.last_test_latency_ms = None
    profile.last_tested_at = None


async def _run_text_probe(
    client: ModelClient,
) -> tuple[TextGeneration | None, ModelTestProbeRead]:
    started = time.perf_counter()
    try:
        result = await client.generate_text(
            "This is a connection test. Reply with a short plain-text acknowledgement.",
            max_output_tokens=64,
        )
        passed = bool(result.text.strip())
        return result, ModelTestProbeRead(
            passed=passed,
            latency_ms=_elapsed_ms(started),
            error=None if passed else "Model returned empty text",
        )
    except ModelClientError as exc:
        return None, ModelTestProbeRead(
            passed=False,
            latency_ms=_elapsed_ms(started),
            error=str(exc),
        )


async def _run_json_probe(
    client: ModelClient,
) -> tuple[JSONGeneration | None, ModelTestProbeRead]:
    started = time.perf_counter()
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean", "const": True}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    try:
        result = await client.generate_json(
            'Return exactly the JSON object {"ok": true}.',
            json_schema=schema,
            max_output_tokens=64,
        )
        passed = result.value == {"ok": True}
        return result, ModelTestProbeRead(
            passed=passed,
            latency_ms=_elapsed_ms(started),
            error=None if passed else "Model did not return the required JSON object",
            native_structured_output=(
                result.native_structured_output if passed else False
            ),
        )
    except ModelClientError as exc:
        return None, ModelTestProbeRead(
            passed=False,
            latency_ms=_elapsed_ms(started),
            error=str(exc),
            native_structured_output=False,
        )


async def _run_vision_probe(
    client: ModelClient,
) -> tuple[TextGeneration | None, ModelTestProbeRead]:
    started = time.perf_counter()
    image_path = Path(__file__).resolve().parents[1] / "static" / "kb-vision-test.png"
    try:
        image_bytes = image_path.read_bytes()
        result = await client.generate_multimodal(
            "Read the exact code shown in this image. Return only that code.",
            image_bytes,
            image_media_type="image/png",
            max_output_tokens=64,
        )
        passed = "KB-VISION-42" in result.text.upper()
        return result, ModelTestProbeRead(
            passed=passed,
            latency_ms=_elapsed_ms(started),
            error=None if passed else "Model did not identify the vision test code",
        )
    except (ModelClientError, OSError) as exc:
        error = str(exc) if isinstance(exc, ModelClientError) else "Vision test image unavailable"
        return None, ModelTestProbeRead(
            passed=False,
            latency_ms=_elapsed_ms(started),
            error=error,
        )


def _tested_protocol_from_results(
    *results: TextGeneration | JSONGeneration | None,
) -> TestedProtocol | None:
    for result in results:
        if result is not None:
            return result.protocol
    return None


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
