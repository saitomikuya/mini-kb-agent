"""Stateless chat-generation and local-history title contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(ChatSchema):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=12_000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("content must not be blank")
        return normalized


class ChatStreamRequest(ChatSchema):
    question: str = Field(max_length=20_000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=12)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized

    @model_validator(mode="after")
    def bound_history_size(self) -> "ChatStreamRequest":
        if sum(len(message.content) for message in self.history) > 48_000:
            raise ValueError("chat history is too large")
        return self


class ChatTitleMessage(ChatMessage):
    pass


class ChatTitleRequest(ChatSchema):
    messages: list[ChatTitleMessage] = Field(min_length=1, max_length=12)


class ChatTitleResponse(ChatSchema):
    title: str
