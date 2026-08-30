"""Unit tests for agent tools."""

import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from langchain_core.runnables import RunnableConfig

from src.tools import (
    calculator,
    get_current_datetime,
    save_note,
    read_notes,
    ALL_TOOLS,
    web_search,
)


TEST_CONFIG: RunnableConfig = {"configurable": {"thread_id": "test-thread"}}


def test_calculator_basic():
    """Test standard arithmetic."""
    result = calculator.invoke({"expression": "2 + 3 * 4"})
    assert "2 + 3 * 4 = 14" in result


def test_calculator_functions():
    """Test math functions like sqrt and power."""
    result = calculator.invoke({"expression": "sqrt(100) + 2 ** 3"})
    assert "= 18.0" in result or "= 18" in result


def test_calculator_safety():
    """Test that unauthorized execution is blocked."""
    result = calculator.invoke({"expression": "__import__('os').system('ls')"})
    assert "エラー" in result


def test_get_current_datetime():
    """Test getting current datetime."""
    result = get_current_datetime.invoke({})
    assert "JST" in result
    assert "UTC" in result


def test_save_and_read_notes(tmp_path, monkeypatch):
    """Test saving and reading notes."""
    temp_notes_file = tmp_path / "notes.json"
    temp_notes_db = tmp_path / "notes.sqlite"
    monkeypatch.setattr("src.tools.NOTES_FILE", temp_notes_file)
    monkeypatch.setattr("src.tools.NOTES_DB", temp_notes_db)
    monkeypatch.setattr("src.tools.DATA_DIR", tmp_path)

    # Save a note
    save_res = save_note.invoke(
        {"title": "テストメモ", "content": "これはテスト内容です。"},
        config=TEST_CONFIG,
    )
    assert "メモを保存しました" in save_res

    # Read notes
    read_res = read_notes.invoke({}, config=TEST_CONFIG)
    other_config: RunnableConfig = {
        "configurable": {"thread_id": "another-thread"}
    }
    other_read_res = read_notes.invoke({}, config=other_config)

    assert "テストメモ" in read_res
    assert "これはテスト内容です。" in read_res
    assert other_read_res == "保存されているメモはありません。"


def test_calculator_blocks_attribute_access_and_large_exponents():
    """Test that non-mathematical AST nodes and expensive powers are blocked."""
    attribute_result = calculator.invoke(
        {"expression": "().__class__.__base__.__subclasses__().__len__()"}
    )
    exponent_result = calculator.invoke({"expression": "2 ** 1000000000"})

    assert "計算エラー" in attribute_result
    assert "計算エラー" in exponent_result


def test_web_search_rejects_oversized_inputs_without_external_access():
    """Validate search bounds before invoking the external search provider."""
    long_query = web_search.invoke({"query": "a" * 501, "max_results": 5})
    too_many = web_search.invoke({"query": "test", "max_results": 11})

    assert "最大500文字" in long_query
    assert "1〜10件" in too_many


def test_web_search_does_not_expose_provider_error(monkeypatch):
    """Keep provider internals and URLs out of tool responses."""

    class FailingSearch:
        def text(self, *_args, **_kwargs):
            raise RuntimeError("internal http://search.internal.local?token=secret")

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FailingSearch))

    result = web_search.invoke({"query": "test", "max_results": 5})

    assert result == "Web検索中にエラーが発生しました。時間をおいて再試行してください。"
    assert "internal.local" not in result


def test_concurrent_note_saves_are_not_lost(tmp_path, monkeypatch):
    """Test that concurrent writers receive unique IDs without lost updates."""
    monkeypatch.setattr("src.tools.NOTES_FILE", tmp_path / "notes.json")
    monkeypatch.setattr("src.tools.NOTES_DB", tmp_path / "notes.sqlite")
    monkeypatch.setattr("src.tools.DATA_DIR", tmp_path)

    def save(index: int) -> str:
        return save_note.invoke(
            {"title": f"メモ{index}", "content": "本文"}, config=TEST_CONFIG
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(save, range(8)))

    read_result = read_notes.invoke({}, config=TEST_CONFIG)
    assert all("メモを保存しました" in result for result in results)
    assert "合計: 8件" in read_result
    assert all(f"メモ{index}" in read_result for index in range(8))


def test_all_tools_exported():
    """Test that ALL_TOOLS contains expected tools."""
    tool_names = [t.name for t in ALL_TOOLS]
    assert "calculator" in tool_names
    assert "get_current_datetime" in tool_names
    assert "web_search" in tool_names
    assert "save_note" in tool_names
    assert "read_notes" in tool_names
