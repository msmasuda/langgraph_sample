"""Streamlit application smoke tests with a mocked shared API."""

import json
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
    assert not app.chat_input
    assert app.text_area[0].disabled is False
    assert any(button.label == "送信" for button in app.button)
    assert any(item.value == conversation["title"] for item in app.subheader)
    get_settings.cache_clear()


def test_streamlit_app_does_not_create_empty_conversation_on_load(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("WEB_API_BASE_URL", "http://api.test")
    get_settings.cache_clear()
    requested_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_methods.append(request.method)
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={"status": "ready", "ollama": True, "database": True},
            )
        if request.url.path == "/v1/conversations" and request.method == "GET":
            return httpx.Response(
                200,
                json={"items": [], "total": 0, "limit": 100, "offset": 0},
            )
        return httpx.Response(500)

    real_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch("src.web_api_client.httpx.Client", side_effect=mock_client):
        app = AppTest.from_file(
            str(Path(__file__).parents[1] / "src" / "web_app.py")
        ).run(timeout=10)

    assert not app.exception
    assert "POST" not in requested_methods
    assert not app.selectbox
    assert any("新しい会話" in item.value for item in app.info)
    get_settings.cache_clear()


def test_streamlit_app_submits_message_only_from_explicit_form(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("WEB_API_BASE_URL", "http://api.test")
    get_settings.cache_clear()
    conversation = {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "IME入力テスト",
        "status": "active",
        "created_at": "2026-08-30T00:00:00Z",
        "updated_at": "2026-08-30T00:00:00Z",
        "expires_at": "2026-11-28T00:00:00Z",
    }
    submitted_messages: list[str] = []

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
        if request.url.path.endswith("/messages/stream"):
            submitted_messages.append(str(json.loads(request.content)["content"]))
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=(
                    "event: assistant.delta\n"
                    'data: {"delta":"回答"}\n\n'
                    "event: message.completed\n"
                    'data: {"content":"回答"}\n\n'
                ),
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"conversation_id": conversation["id"], "items": []},
            )
        return httpx.Response(500)

    real_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    with patch("src.web_api_client.httpx.Client", side_effect=mock_client):
        app = AppTest.from_file(
            str(Path(__file__).parents[1] / "src" / "web_app.py")
        ).run(timeout=10)
        app.text_area[0].input("こんにちは")
        next(button for button in app.button if button.label == "送信").click()
        app.run(timeout=10)

    assert not app.exception
    assert submitted_messages == ["こんにちは"]
    get_settings.cache_clear()
