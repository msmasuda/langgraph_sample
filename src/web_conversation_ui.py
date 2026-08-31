"""Pure helpers for stable, distinguishable Streamlit conversation selection."""

from __future__ import annotations

from collections.abc import Sequence

from src.api.schemas import ConversationResponse


DEFAULT_CONVERSATION_TITLE = "新しい会話"


def resolve_conversation_id(
    conversation_ids: Sequence[str],
    *,
    next_id: str | None = None,
    query_id: str | None = None,
    current_id: str | None = None,
) -> str | None:
    """Resolve one valid conversation ID using explicit navigation first."""
    valid_ids = set(conversation_ids)
    for candidate in (next_id, query_id, current_id):
        if candidate in valid_ids:
            return candidate
    return conversation_ids[0] if conversation_ids else None


def conversation_option_label(conversation: ConversationResponse) -> str:
    """Return a compact label that remains unique when titles are duplicated."""
    updated_at = conversation.updated_at.astimezone().strftime("%m/%d %H:%M")
    short_id = str(conversation.id)[:8]
    return f"{conversation.title} · {updated_at} · {short_id}"


def title_from_prompt(prompt: str, *, max_chars: int = 40) -> str:
    """Create a bounded one-line conversation title from the first prompt."""
    normalized = " ".join(prompt.split()).strip()
    if not normalized:
        return DEFAULT_CONVERSATION_TITLE
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max_chars - 1].rstrip()}…"
