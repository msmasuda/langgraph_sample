"""Server-Sent Events encoding helpers."""

import json
from collections.abc import Mapping
from typing import Any


def encode_sse(
    event: str,
    data: Mapping[str, Any],
    *,
    event_id: str | None = None,
) -> str:
    """Encode one SSE event with compact single-line JSON data."""
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"
