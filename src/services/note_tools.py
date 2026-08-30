"""Database-backed note tools used by the PostgreSQL API runtime."""

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool


def _thread_id(config: RunnableConfig) -> str:
    value = str(config.get("configurable", {}).get("thread_id", "")).strip()
    if not value or len(value) > 200:
        raise ValueError("有効なスレッドIDが必要です")
    return value


def create_database_note_tools(
    conversation_store: Any,
    note_store: Any,
    *,
    user_id: uuid.UUID,
) -> list[BaseTool]:
    """Build async note tools sharing the API's PostgreSQL repositories."""

    @tool("save_note")
    async def save_database_note(
        title: str,
        content: str,
        config: RunnableConfig,
    ) -> str:
        """Save a note for the current conversation."""
        if not title.strip():
            return "メモ保存エラー: タイトルを入力してください。"
        if len(title) > 200 or len(content) > 20_000:
            return "メモ保存エラー: タイトルまたは本文が長すぎます。"
        try:
            conversation = await conversation_store.get_by_thread_id(
                _thread_id(config),
                user_id=user_id,
            )
            if conversation is None:
                return "メモ保存エラー: 会話が見つかりません。"
            note = await note_store.create(
                conversation.id,
                user_id=user_id,
                title=title.strip(),
                content=content.strip(),
            )
            return f"メモを保存しました (ID: {note.id}, タイトル: '{note.title}')"
        except Exception:
            return "メモ保存エラー: データベースへ保存できませんでした。"

    @tool("read_notes")
    async def read_database_notes(config: RunnableConfig) -> str:
        """Read notes saved for the current conversation."""
        try:
            conversation = await conversation_store.get_by_thread_id(
                _thread_id(config),
                user_id=user_id,
            )
            if conversation is None:
                return "メモ読み出しエラー: 会話が見つかりません。"
            notes = await note_store.list(conversation.id, user_id=user_id)
            if not notes:
                return "保存されているメモはありません。"
            lines = [f"【保存されているメモ一覧 (合計: {len(notes)}件)】\n"]
            for note in notes:
                lines.append(
                    f"- [ID: {note.id}] {note.title} ({note.created_at.isoformat()})\n"
                    f"  内容: {note.content}\n"
                )
            return "\n".join(lines)
        except Exception:
            return (
                "メモ読み出しエラー: "
                "データベースを読み出せませんでした。"
            )

    return [save_database_note, read_database_notes]
