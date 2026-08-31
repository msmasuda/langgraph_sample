"""Tests for stable Streamlit conversation selection helpers."""

import uuid
from datetime import UTC, datetime, timedelta

from src.api.schemas import ConversationResponse
from src.web_conversation_ui import (
    DEFAULT_CONVERSATION_TITLE,
    conversation_option_label,
    resolve_conversation_id,
    title_from_prompt,
)


def conversation(
    conversation_id: str = "00000000-0000-0000-0000-000000000001",
    *,
    title: str = DEFAULT_CONVERSATION_TITLE,
) -> ConversationResponse:
    created_at = datetime(2026, 8, 31, tzinfo=UTC)
    return ConversationResponse(
        id=uuid.UUID(conversation_id),
        title=title,
        status="active",
        created_at=created_at,
        updated_at=created_at + timedelta(hours=1),
        expires_at=created_at + timedelta(days=90),
    )


def test_query_conversation_wins_over_stale_session_selection():
    assert resolve_conversation_id(
        ["conversation-1", "conversation-2"],
        query_id="conversation-2",
        current_id="conversation-1",
    ) == "conversation-2"


def test_explicit_next_conversation_wins_over_query_selection():
    assert resolve_conversation_id(
        ["conversation-1", "conversation-2"],
        next_id="conversation-1",
        query_id="conversation-2",
    ) == "conversation-1"


def test_selection_falls_back_without_creating_a_conversation():
    assert resolve_conversation_id([], query_id="missing") is None
    assert resolve_conversation_id(
        ["conversation-1"],
        query_id="missing",
        current_id="also-missing",
    ) == "conversation-1"


def test_duplicate_titles_have_distinguishable_labels():
    first = conversation("11111111-0000-0000-0000-000000000001")
    second = conversation("22222222-0000-0000-0000-000000000002")

    first_label = conversation_option_label(first)
    second_label = conversation_option_label(second)

    assert first_label != second_label
    assert DEFAULT_CONVERSATION_TITLE in first_label
    assert "11111111" in first_label


def test_title_is_generated_from_first_prompt():
    assert title_from_prompt("  株式会社GUMIについて\n教えてください  ") == (
        "株式会社GUMIについて 教えてください"
    )
    assert title_from_prompt("あ" * 50) == ("あ" * 39) + "…"
    assert title_from_prompt("   ") == DEFAULT_CONVERSATION_TITLE
