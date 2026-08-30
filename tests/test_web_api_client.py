"""Tests for the Streamlit-facing FastAPI client."""

import json

import httpx
import pytest

from src.web_api_client import (
    AgentApiClient,
    AgentApiError,
    parse_sse_lines,
)


def test_client_sends_bearer_token_and_parses_conversations():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer access-token"
        assert request.headers["X-Request-ID"]
        assert request.url.params["limit"] == "100"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "title": "会話",
                        "status": "active",
                        "created_at": "2026-08-30T00:00:00Z",
                        "updated_at": "2026-08-30T00:00:00Z",
                        "expires_at": "2026-11-28T00:00:00Z",
                    }
                ],
                "total": 1,
                "limit": 100,
                "offset": 0,
            },
        )

    client = AgentApiClient(
        "http://api.test/",
        access_token="access-token",
        transport=httpx.MockTransport(handler),
    )

    conversations = client.list_conversations()

    assert conversations.total == 1
    assert conversations.items[0].title == "会話"


def test_client_exposes_only_safe_api_error_fields():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "12", "X-Request-ID": "request-1"},
            json={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "時間をおいて再試行してください。",
                    "request_id": "request-1",
                }
            },
        )

    client = AgentApiClient(
        "http://api.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentApiError) as caught:
        client.list_models()

    assert caught.value.code == "rate_limit_exceeded"
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 12
    assert caught.value.request_id == "request-1"


def test_client_hides_non_json_server_error_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text="postgresql://user:secret@database.internal/langgraph",
        )

    client = AgentApiClient(
        "http://api.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentApiError) as caught:
        client.list_models()

    assert "database.internal" not in caught.value.user_message
    assert caught.value.code == "api_request_failed"


def test_client_rejects_invalid_success_payload_safely():
    client = AgentApiClient(
        "http://api.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"unexpected": "payload"})
        ),
    )

    with pytest.raises(AgentApiError) as caught:
        client.list_models()

    assert caught.value.code == "invalid_api_response"
    assert "unexpected" not in caught.value.user_message


def test_parse_sse_lines_supports_comments_and_multiline_json():
    events = list(
        parse_sse_lines(
            [
                ": keep-alive",
                "id: message-1:1",
                "event: assistant.delta",
                'data: {"message_id":"message-1",',
                'data: "delta":"回答"}',
                "",
            ]
        )
    )

    assert len(events) == 1
    assert events[0].event == "assistant.delta"
    assert events[0].event_id == "message-1:1"
    assert events[0].data["delta"] == "回答"


def test_stream_message_sends_idempotency_key_and_decodes_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "message-key"
        assert request.headers["Accept"] == "text/event-stream"
        assert json.loads(request.content) == {"content": "質問"}
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

    client = AgentApiClient(
        "http://api.test",
        transport=httpx.MockTransport(handler),
    )

    events = list(
        client.stream_message(
            "00000000-0000-0000-0000-000000000001",
            "質問",
            idempotency_key="message-key",
        )
    )

    assert [event.event for event in events] == [
        "assistant.delta",
        "message.completed",
    ]


def test_client_maps_connection_failure_to_safe_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("http://database.internal", request=request)

    client = AgentApiClient(
        "http://api.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentApiError) as caught:
        client.ready()

    assert caught.value.code == "api_unavailable"
    assert "database.internal" not in caught.value.user_message
