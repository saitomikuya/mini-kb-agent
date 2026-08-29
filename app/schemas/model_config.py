"""Validation and response schemas for model configuration APIs."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.llm.types import (
    AzureMode,
    ModelRole,
    ModelTestStatus,
    ProtocolPreference,
    ProviderType,
    TestedProtocol,
)


_RESERVED_AUTH_HEADERS = {
    "authorization",
    "proxy-authorization",
    "api-key",
    "x-api-key",
}

# Compatibility-first defaults used when the simplified UI creates a profile.
DEFAULT_MODEL_CONTEXT_WINDOW = 32_768
DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 4_096


def _validate_extra_headers(headers: dict[str, str]) -> dict[str, str]:
    if any(name.lower() in _RESERVED_AUTH_HEADERS for name in headers):
        raise ValueError(
            "authentication headers must use the encrypted api_key field"
        )
    return headers


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class APIProviderCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=200)
    provider_type: ProviderType
    base_url: str = Field(min_length=1)
    api_key: SecretStr
    protocol_preference: ProtocolPreference = ProtocolPreference.AUTO
    extra_headers_json: dict[str, str] = Field(default_factory=dict)
    azure_mode: AzureMode = AzureMode.V1
    azure_api_version: str | None = Field(default=None, max_length=64)
    enabled: bool = True

    @field_validator("name", "base_url")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("api_key")
    @classmethod
    def reject_empty_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("API key must not be empty")
        return value

    @field_validator("extra_headers_json")
    @classmethod
    def reject_auth_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_extra_headers(value)


class APIProviderUpdate(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    provider_type: ProviderType | None = None
    base_url: str | None = Field(default=None, min_length=1)
    api_key: SecretStr | None = None
    protocol_preference: ProtocolPreference | None = None
    extra_headers_json: dict[str, str] | None = None
    azure_mode: AzureMode | None = None
    azure_api_version: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None

    @field_validator("name", "base_url")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("api_key")
    @classmethod
    def reject_empty_optional_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("API key must not be empty")
        return value

    @field_validator("extra_headers_json")
    @classmethod
    def reject_optional_auth_headers(
        cls,
        value: dict[str, str] | None,
    ) -> dict[str, str] | None:
        return _validate_extra_headers(value) if value is not None else None


class APIProviderRead(StrictSchema):
    id: int
    name: str
    provider_type: ProviderType
    base_url: str
    api_key_masked: str
    protocol_preference: ProtocolPreference
    extra_headers_json: dict[str, str]
    azure_mode: AzureMode
    azure_api_version: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ModelProfileCreate(StrictSchema):
    provider_id: int
    name: str = Field(min_length=1, max_length=200)
    remote_model_name: str = Field(min_length=1, max_length=300)
    protocol_override: ProtocolPreference | None = None
    context_window: int | None = Field(default=DEFAULT_MODEL_CONTEXT_WINDOW, gt=0)
    max_output_tokens: int | None = Field(
        default=DEFAULT_MODEL_MAX_OUTPUT_TOKENS,
        gt=0,
    )
    reasoning_effort: str | None = Field(default=None, max_length=32)
    extra_request_json: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name", "remote_model_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class ModelProfileUpdate(StrictSchema):
    provider_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    remote_model_name: str | None = Field(default=None, min_length=1, max_length=300)
    protocol_override: ProtocolPreference | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: str | None = Field(default=None, max_length=32)
    extra_request_json: dict[str, Any] | None = None
    enabled: bool | None = None

    @field_validator("name", "remote_model_name")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class ModelProfileRead(StrictSchema):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    provider_id: int
    name: str
    remote_model_name: str
    protocol_override: ProtocolPreference | None
    context_window: int | None
    max_output_tokens: int | None
    reasoning_effort: str | None
    extra_request_json: dict[str, Any]
    supports_text: bool
    supports_vision: bool
    supports_structured_output: bool
    tested_protocol: TestedProtocol | None
    last_test_status: ModelTestStatus | None
    last_test_latency_ms: int | None
    last_tested_at: datetime | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ModelRoleBindingUpdate(StrictSchema):
    model_profile_id: int | None


class ModelRolePromptUpdate(StrictSchema):
    prompts: dict[str, str]


class ModelRolePromptTaskRead(StrictSchema):
    task: str
    name: str
    description: str
    prompt: str
    default_prompt: str


class ModelRoleBindingRead(StrictSchema):
    role: ModelRole
    model_profile_id: int | None
    updated_at: datetime | None
    prompts_updated_at: datetime | None
    prompt_tasks: list[ModelRolePromptTaskRead]


class ModelTestProbeRead(StrictSchema):
    passed: bool
    latency_ms: int | None = None
    error: str | None = None
    native_structured_output: bool | None = None


class ModelProfileTestRead(StrictSchema):
    model_profile_id: int
    status: ModelTestStatus
    tested_protocol: TestedProtocol | None
    latency_ms: int
    supports_text: bool
    supports_vision: bool
    supports_structured_output: bool
    text: ModelTestProbeRead
    json_probe: ModelTestProbeRead = Field(alias="json")
    vision: ModelTestProbeRead
