"""Pydantic request and response schemas for the public API."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    """API liveness response."""

    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    """API dependency readiness response."""

    status: Literal["ready", "not_ready"]
    ollama: bool


class ModelListResponse(BaseModel):
    """Installed Ollama model names."""

    models: list[str]


class ConversationResponse(BaseModel):
    """Created conversation metadata."""

    id: UUID
    created_at: datetime


class MessageRequest(BaseModel):
    """User message submitted to an agent conversation."""

    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        """Reject whitespace-only messages while preserving user formatting."""
        if not value.strip():
            raise ValueError("メッセージを入力してください")
        return value


class ToolExecutionResponse(BaseModel):
    """Tool execution included in a completed message response."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: str | None = None


class MessageResponse(BaseModel):
    """Completed non-streaming assistant message."""

    id: UUID
    conversation_id: UUID
    content: str
    tool_events: list[ToolExecutionResponse] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """Safe machine- and user-readable API error."""

    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """Top-level API error envelope."""

    error: ErrorDetail
