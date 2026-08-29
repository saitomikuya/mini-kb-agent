"""Stateless chat-generation and local-history title contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatStreamRequest(ChatSchema):
    question: str = Field(max_length=20_000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized


class ChatTitleMessage(ChatSchema):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=12_000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("content must not be blank")
        return normalized


class ChatTitleRequest(ChatSchema):
    messages: list[ChatTitleMessage] = Field(min_length=1, max_length=12)


class ChatTitleResponse(ChatSchema):
    title: str
