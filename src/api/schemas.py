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
    database: bool | None = None


class ModelListResponse(BaseModel):
    """Installed Ollama model names."""

    models: list[str]


class ConversationResponse(BaseModel):
    """Conversation metadata."""

    id: UUID
    title: str
    status: Literal["active", "archived"]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class ConversationCreateRequest(BaseModel):
    """Optional metadata for a new conversation."""

    title: str = Field(default="新しい会話", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("会話タイトルを入力してください")
        return value.strip()


class ConversationUpdateRequest(BaseModel):
    """Mutable conversation metadata."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "archived"] | None = None

    @field_validator("title")
    @classmethod
    def optional_title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("会話タイトルを入力してください")
        return value.strip() if value is not None else None


class ConversationListResponse(BaseModel):
    """Paginated conversations owned by the current user."""

    items: list[ConversationResponse]
    total: int
    limit: int
    offset: int


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


class MessageHistoryItem(BaseModel):
    """One persisted LangGraph message exposed to clients."""

    id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class MessageHistoryResponse(BaseModel):
    """Persisted messages for one conversation."""

    conversation_id: UUID
    items: list[MessageHistoryItem]


class CancelResponse(BaseModel):
    """Cancellation request result."""

    conversation_id: UUID
    status: Literal["cancellation_requested", "not_running"]


class NoteCreateRequest(BaseModel):
    """New conversation note."""

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("title", "content")
    @classmethod
    def note_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("空の値は指定できません")
        return value.strip()


class NoteUpdateRequest(BaseModel):
    """Mutable note fields."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=20_000)

    @field_validator("title", "content")
    @classmethod
    def optional_note_fields_must_not_be_blank(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("空の値は指定できません")
        return value.strip() if value is not None else None


class NoteResponse(BaseModel):
    """Persisted conversation note."""

    id: UUID
    conversation_id: UUID
    title: str
    content: str
    created_at: datetime
    updated_at: datetime


class NoteListResponse(BaseModel):
    """Notes for one conversation."""

    items: list[NoteResponse]


class ErrorDetail(BaseModel):
    """Safe machine- and user-readable API error."""

    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """Top-level API error envelope."""

    error: ErrorDetail
