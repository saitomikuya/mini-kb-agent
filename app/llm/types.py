"""Stable model-provider, protocol, and application-role identifiers."""

from enum import StrEnum


class ProviderType(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    AZURE_OPENAI = "azure_openai"
    SUB2API = "sub2api"


class ProtocolPreference(StrEnum):
    AUTO = "auto"
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class TestedProtocol(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class AzureMode(StrEnum):
    V1 = "v1"
    LEGACY = "legacy"


class ModelRole(StrEnum):
    DOCUMENT_CONVERSION = "document_conversion"
    INDEX_GENERATION = "index_generation"
    QUERY_ROUTER = "query_router"
    ANSWER_GENERATION = "answer_generation"


class ModelTestStatus(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"


ALL_MODEL_ROLES = tuple(ModelRole)
