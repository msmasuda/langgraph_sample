"""Streamlit application smoke tests with a mocked shared API."""

from pathlib import Path
from unittest.mock import patch

import httpx
from streamlit.testing.v1 import AppTest

from src.config import get_settings


def test_streamlit_app_loads_conversation_from_api(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("WEB_API_BASE_URL", "http://api.test")
    get_settings.cache_clear()
    conversation = {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "APIから取得した会話",
        "status": "active",
        "created_at": "2026-08-30T00:00:00Z",
        "updated_at": "2026-08-30T00:00:00Z",
        "expires_at": "2026-11-28T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={"status": "ready", "ollama": True, "database": True},
            )
        if request.url.path == "/v1/conversations" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [conversation],
                    "total": 1,
                    "limit": 100,
                    "offset": 0,
                },
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"conversation_id": conversation["id"], "items": []},
            )
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "not_found",
                    "message": "見つかりません。",
                    "request_id": "request-1",
                }
            },
        )

    real_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch("src.web_api_client.httpx.Client", side_effect=mock_client):
        app = AppTest.from_file(
            str(Path(__file__).parents[1] / "src" / "web_app.py")
        ).run(timeout=10)

    assert not app.exception
    assert app.title[0].value == "LangGraph + Ollama エージェント"
    assert app.selectbox[0].value == conversation["id"]
    assert app.chat_input[0].disabled is False
    assert any(item.value == conversation["title"] for item in app.subheader)
    get_settings.cache_clear()
