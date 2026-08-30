"""Validated admin contract for knowledge-query tuning."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import MAX_CONFIGURABLE_OUTPUT_TOKENS


class KnowledgeTuningValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_router_context_tokens: int = Field(ge=4_096, le=2_000_000)
    navigation_default_max_output_tokens: int = Field(
        ge=256,
        le=MAX_CONFIGURABLE_OUTPUT_TOKENS,
    )
    answer_context_tokens: int = Field(ge=4_096, le=2_000_000)
    answer_max_output_tokens: int = Field(
        ge=256,
        le=MAX_CONFIGURABLE_OUTPUT_TOKENS,
    )
    navigation_root_input_token_cap: int = Field(ge=1_024, le=1_000_000)
    navigation_folder_input_token_cap: int = Field(ge=1_024, le=1_000_000)
    navigation_context_safety_percent: int = Field(ge=1, le=25)
    navigation_max_selected_documents: int = Field(ge=1, le=100)
    navigation_max_selected_parts: int = Field(ge=1, le=200)
    lexical_candidate_parts: int = Field(ge=1, le=200)
    lexical_fallback_parts: int = Field(ge=1, le=50)
    lexical_max_parts_per_document: int = Field(ge=1, le=100)
    navigation_low_confidence_percent: int = Field(ge=1, le=100)
    answer_verbosity: Literal["low", "medium", "high"]
    document_text_chars_per_part: int = Field(ge=1_000, le=100_000)
    document_excel_rows_per_part: int = Field(ge=10, le=2_000)
    root_max_document_types: int = Field(ge=1, le=64)
    root_max_topics: int = Field(ge=1, le=128)
    root_max_entities: int = Field(ge=1, le=128)
    root_max_representative_titles: int = Field(ge=1, le=64)
    folder_summary_topics: int = Field(ge=1, le=32)

    @model_validator(mode="after")
    def validate_relationships(self) -> "KnowledgeTuningValues":
        if self.lexical_fallback_parts > self.navigation_max_selected_parts:
            raise ValueError("词法兜底 part 数不能超过回答 part 上限")
        if self.lexical_max_parts_per_document > self.lexical_candidate_parts:
            raise ValueError("单文档候选数不能超过总词法候选数")
        if self.navigation_default_max_output_tokens >= self.query_router_context_tokens:
            raise ValueError("路由输出必须小于路由上下文")
        if self.answer_max_output_tokens >= self.answer_context_tokens:
            raise ValueError("回答输出必须小于回答上下文")
        return self


class KnowledgeTuningRead(KnowledgeTuningValues):
    updated_at: datetime | None = None
