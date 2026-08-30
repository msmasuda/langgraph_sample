"""Tools for LangGraph agent."""

import ast
import datetime
import json
import math
import sqlite3
from pathlib import Path
from typing import Any
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

# Notes storage directory and file path
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NOTES_FILE = DATA_DIR / "notes.json"  # Legacy storage imported on first use.
NOTES_DB = DATA_DIR / "notes.sqlite"


def _connect_notes_db() -> sqlite3.Connection:
    """Open and initialize the SQLite notes store."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(NOTES_DB, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    if "thread_id" not in columns:
        conn.execute(
            "ALTER TABLE notes ADD COLUMN thread_id TEXT NOT NULL DEFAULT 'default-session'"
        )
    _import_legacy_notes(conn)
    conn.commit()
    return conn


def _import_legacy_notes(conn: sqlite3.Connection) -> None:
    """Import valid legacy JSON notes once when the database is empty."""
    if not NOTES_FILE.is_file():
        return
    if conn.execute("SELECT 1 FROM notes LIMIT 1").fetchone() is not None:
        return

    try:
        with NOTES_FILE.open(encoding="utf-8") as file:
            legacy_notes = json.load(file)
    except (OSError, json.JSONDecodeError):
        return

    if not isinstance(legacy_notes, list):
        return

    rows = []
    for note in legacy_notes:
        if not isinstance(note, dict):
            continue
        title = note.get("title")
        content = note.get("content")
        created_at = note.get("created_at")
        if all(isinstance(value, str) for value in (title, content, created_at)):
            rows.append(("default-session", title, content, created_at))

    if rows:
        conn.executemany(
            "INSERT INTO notes (thread_id, title, content, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )


_MATH_FUNCTIONS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "ceil": math.ceil,
    "floor": math.floor,
    "abs": abs,
    "round": round,
}
_MATH_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_INTEGER_BITS = 4096
_MAX_EXPONENT = 1000


def _validate_number(value: Any) -> None:
    """Reject non-numeric, non-finite, or excessively large results."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("数値以外の結果は使用できません")
    if isinstance(value, int) and value.bit_length() > _MAX_INTEGER_BITS:
        raise ValueError("計算結果が大きすぎます")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("計算結果が有限値ではありません")


def _evaluate_math_node(node: ast.AST) -> int | float:
    """Evaluate an allowlisted mathematical AST."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("数値以外の定数は使用できません")
        _validate_number(value)
        return value

    if isinstance(node, ast.Name) and node.id in _MATH_CONSTANTS:
        return _MATH_CONSTANTS[node.id]

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _evaluate_math_node(node.operand)
        result = operand if isinstance(node.op, ast.UAdd) else -operand
        _validate_number(result)
        return result

    if isinstance(node, ast.BinOp):
        left = _evaluate_math_node(node.left)
        right = _evaluate_math_node(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_EXPONENT:
                raise ValueError(f"指数は±{_MAX_EXPONENT}以下にしてください")
            if isinstance(left, int) and right >= 0 and left.bit_length() * right > _MAX_INTEGER_BITS:
                raise ValueError("計算結果が大きすぎます")
            result = left**right
        elif isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            result = left // right
        elif isinstance(node.op, ast.Mod):
            result = left % right
        else:
            raise ValueError("許可されていない演算です")
        _validate_number(result)
        return result

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        function = _MATH_FUNCTIONS.get(node.func.id)
        if function is None or node.keywords:
            raise ValueError("許可されていない関数です")
        if len(node.args) not in (1, 2) or (len(node.args) == 2 and node.func.id not in {"log", "round"}):
            raise ValueError("関数の引数の数が正しくありません")
        arguments = [_evaluate_math_node(argument) for argument in node.args]
        result = function(*arguments)
        _validate_number(result)
        return result

    raise ValueError("許可されていない式です")


@tool
def get_current_datetime() -> str:
    """Get the current date, time, and day of the week in Japan Standard Time (JST) and UTC.

    Returns:
        Formatted string containing current date, time, and timezone information.
    """
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    jst_tz = datetime.timezone(datetime.timedelta(hours=9))
    jst_now = utc_now.astimezone(jst_tz)

    weekdays_ja = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
    weekday_ja = weekdays_ja[jst_now.weekday()]

    return (
        f"【現在日時 (JST)】: {jst_now.strftime('%Y年%m月%d日')} ({weekday_ja}) {jst_now.strftime('%H時%M分%S秒')}\n"
        f"【UTC】: {utc_now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )


@tool
def calculator(expression: str) -> str:
    """Safely calculate mathematical expressions.

    Supported operations: +, -, *, /, //, %, **, sqrt, sin, cos, tan, log, log10, exp, ceil, floor, abs, round, pi, e.
    Example expressions: '3 * 4 + 10', 'sqrt(144)', '2 ** 8', 'sin(pi / 2)'

    Args:
        expression: Mathematical expression string to evaluate.

    Returns:
        The evaluated result as a string or an error message.
    """
    cleaned_expr = expression.strip().replace("^", "**")
    if not cleaned_expr:
        return "計算エラー: 式が空です。"
    if len(cleaned_expr) > 200:
        return "計算エラー: 式が長すぎます（最大200文字）。"

    try:
        tree = ast.parse(cleaned_expr, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 100:
            raise ValueError("式が複雑すぎます")
        result = _evaluate_math_node(tree.body)
        _validate_number(result)
        return f"{expression} = {result}"
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError) as error:
        return f"計算エラー: {error} (式: {expression})"


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for real-time information, news, or knowledge using DuckDuckGo.

    Args:
        query: Search query string.
        max_results: Maximum number of search results to return (default: 5).

    Returns:
        Formatted search results with title, link, and snippet.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        ddgs = DDGS()
        # Try search with Japanese region first, then fallback if empty
        results = list(ddgs.text(query, region="jp-jp", max_results=max_results))
        if not results:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return f"検索結果が見つかりませんでした: '{query}'"

        formatted_results = [f"【Web検索結果: '{query}'】\n"]
        for idx, item in enumerate(results, 1):
            title = item.get("title", "No Title")
            href = item.get("href", "")
            body = item.get("body", "")
            formatted_results.append(
                f"[{idx}] {title}\nURL: {href}\n概要: {body}\n"
            )

        return "\n".join(formatted_results)
    except Exception as e:
        return f"Web検索中にエラーが発生しました: {str(e)}"


def _get_thread_id(config: RunnableConfig) -> str:
    """Extract a bounded checkpoint thread ID from runnable configuration."""
    thread_id = str(config.get("configurable", {}).get("thread_id", "")).strip()
    if not thread_id or len(thread_id) > 200:
        raise ValueError("有効なスレッドIDが必要です")
    return thread_id


@tool
def save_note(title: str, content: str, config: RunnableConfig) -> str:
    """Save a note or memo to the local storage.

    Args:
        title: Title or category of the note.
        content: Detailed content of the note.

    Returns:
        Confirmation message.
    """
    if not title.strip():
        return "メモ保存エラー: タイトルを入力してください。"
    if len(title) > 200 or len(content) > 20_000:
        return "メモ保存エラー: タイトルまたは本文が長すぎます。"

    try:
        thread_id = _get_thread_id(config)
        now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with _connect_notes_db() as conn:
            cursor = conn.execute(
                "INSERT INTO notes (thread_id, title, content, created_at) VALUES (?, ?, ?, ?)",
                (thread_id, title, content, now),
            )
            note_id = cursor.lastrowid
        return f"メモを保存しました (ID: {note_id}, タイトル: '{title}')"
    except ValueError as error:
        return f"メモ保存エラー: {error}"
    except sqlite3.Error:
        return "メモ保存エラー: データベースへ保存できませんでした。"


@tool
def read_notes(config: RunnableConfig) -> str:
    """Read all saved notes and memos from the local storage.

    Returns:
        List of all saved notes or a message indicating no notes exist.
    """
    try:
        thread_id = _get_thread_id(config)
        with _connect_notes_db() as conn:
            notes = conn.execute(
                "SELECT id, title, content, created_at FROM notes WHERE thread_id = ? ORDER BY id",
                (thread_id,),
            ).fetchall()

        if not notes:
            return "保存されているメモはありません。"

        result = [f"【保存されているメモ一覧 (合計: {len(notes)}件)】\n"]
        for note_id, title, content, created_at in notes:
            result.append(
                f"- [ID: {note_id}] {title} ({created_at})\n  内容: {content}\n"
            )
        return "\n".join(result)
    except ValueError as error:
        return f"メモ読み出しエラー: {error}"
    except sqlite3.Error:
        return "メモ読み出しエラー: データベースを読み出せませんでした。"


# Exported list of all tools
ALL_TOOLS = [
    get_current_datetime,
    calculator,
    web_search,
    save_note,
    read_notes,
]
