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


class ReasoningEffort(StrEnum):
    """Role-level reasoning choices exposed by the administration UI."""

    MODEL_DEFAULT = "model_default"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ModelTestStatus(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"


ALL_MODEL_ROLES = tuple(ModelRole)

DEFAULT_ROLE_REASONING_EFFORTS = {
    ModelRole.DOCUMENT_CONVERSION: ReasoningEffort.LOW,
    ModelRole.INDEX_GENERATION: ReasoningEffort.LOW,
    ModelRole.QUERY_ROUTER: ReasoningEffort.MODEL_DEFAULT,
    ModelRole.ANSWER_GENERATION: ReasoningEffort.MODEL_DEFAULT,
}
