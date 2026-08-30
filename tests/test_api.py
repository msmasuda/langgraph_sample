"""Integration tests for the FastAPI application."""

import asyncio
import uuid

import httpx
import pytest
from fastapi import Request

from src.api.app import create_app
from src.api.schemas import MessageRequest
from src.config import Settings
from src.errors import AgentConnectionError, AgentTimeoutError
from src.services import (
    AgentEvent,
    AgentRunResult,
    ConversationExecutionRegistry,
    InMemoryConversationStore,
    OllamaStatus,
)


class StubModelService:
    """Return a predefined Ollama status."""

    def __init__(self, available: bool = True) -> None:
        self.status = OllamaStatus(
            available=available,
            models=("model-a", "model-b") if available else (),
        )

    async def aget_status(self) -> OllamaStatus:
        return self.status


class StubAgentService:
    """Return deterministic completed and streaming agent events."""

    def __init__(self) -> None:
        self.invoke_calls = 0
        self.stream_calls = 0

    async def ainvoke(self, prompt: str, thread_id: str) -> AgentRunResult:
        self.invoke_calls += 1
        assert prompt
        assert uuid.UUID(thread_id)
        events = (
            AgentEvent(
                type="tool_started",
                node_name="chatbot",
                tool_name="calculator",
                tool_args={"expression": "1 + 1"},
                tool_call_id="call-1",
            ),
            AgentEvent(
                type="tool_completed",
                node_name="tools",
                tool_name="calculator",
                content="1 + 1 = 2",
                tool_call_id="call-1",
            ),
            AgentEvent(
                type="assistant_completed",
                node_name="chatbot",
                content="答えは2です。",
            ),
        )
        return AgentRunResult(response="答えは2です。", events=events)

    async def astream_events(
        self,
        prompt: str,
        thread_id: str,
        *,
        include_tokens: bool = False,
    ):
        self.stream_calls += 1
        assert prompt
        assert uuid.UUID(thread_id)
        assert include_tokens is True
        yield AgentEvent(
            type="assistant_delta",
            node_name="chatbot",
            content="答えは",
        )
        yield AgentEvent(
            type="tool_started",
            node_name="chatbot",
            tool_name="calculator",
            tool_args={"expression": "1 + 1"},
            tool_call_id="call-1",
        )
        yield AgentEvent(
            type="tool_completed",
            node_name="tools",
            tool_name="calculator",
            content="1 + 1 = 2",
            tool_call_id="call-1",
        )
        yield AgentEvent(
            type="assistant_delta",
            node_name="chatbot",
            content="2です。",
        )
        yield AgentEvent(
            type="assistant_completed",
            node_name="chatbot",
            content="答えは2です。",
        )


def make_test_app(
    *,
    agent_service=None,
    model_available: bool = True,
    conversation_store=None,
    execution_registry=None,
):
    return create_app(
        settings=Settings(
            api_max_message_chars=20_000,
            idempotency_ttl_seconds=60,
            idempotency_max_entries=20,
        ),
        agent_service=agent_service or StubAgentService(),
        model_service=StubModelService(model_available),
        conversation_store=conversation_store,
        execution_registry=execution_registry,
    )


async def create_conversation(client: httpx.AsyncClient) -> str:
    response = await client.post("/v1/conversations")
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_health_request_id_and_openapi_paths():
    app = make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/health",
            headers={"X-Request-ID": "test-request-1"},
        )
        openapi = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "test-request-1"
    expected_paths = {
        "/health",
        "/ready",
        "/v1/models",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}/messages",
        "/v1/conversations/{conversation_id}/messages/stream",
    }
    assert expected_paths.issubset(openapi.json()["paths"])


@pytest.mark.asyncio
async def test_readiness_and_models_when_ollama_is_unavailable():
    app = make_test_app(model_available=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        ready = await client.get("/ready")
        models = await client.get("/v1/models")

    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "ollama": False}
    assert models.status_code == 503
    assert models.json()["error"]["code"] == "ollama_unavailable"


@pytest.mark.asyncio
async def test_models_returns_installed_model_names():
    app = make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": ["model-a", "model-b"]}


@pytest.mark.asyncio
async def test_non_streaming_message_and_idempotent_replay():
    agent = StubAgentService()
    app = make_test_app(agent_service=agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        conversation_id = await create_conversation(client)
        url = f"/v1/conversations/{conversation_id}/messages"
        headers = {"Idempotency-Key": "message-key-1"}
        first = await client.post(
            url,
            json={"content": "1 + 1は？"},
            headers=headers,
        )
        replay = await client.post(
            url,
            json={"content": "1 + 1は？"},
            headers=headers,
        )
        conflict = await client.post(
            url,
            json={"content": "別の質問"},
            headers=headers,
        )

    assert first.status_code == 200
    assert first.json()["content"] == "答えは2です。"
    assert first.json()["tool_events"][0]["output"] == "1 + 1 = 2"
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert agent.invoke_calls == 1


@pytest.mark.asyncio
async def test_unknown_conversation_returns_safe_error():
    app = make_test_app()
    conversation_id = uuid.uuid4()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            json={"content": "質問"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_non_streaming_agent_error_uses_safe_envelope():
    class FailingAgent(StubAgentService):
        async def ainvoke(self, prompt: str, thread_id: str) -> AgentRunResult:
            raise AgentConnectionError() from RuntimeError("internal host detail")

    app = make_test_app(agent_service=FailingAgent())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        conversation_id = await create_conversation(client)
        response = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            json={"content": "質問"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agent_connection_error"
    assert "internal host detail" not in response.text


@pytest.mark.asyncio
async def test_sse_stream_contains_all_public_event_types_and_replays():
    agent = StubAgentService()
    app = make_test_app(agent_service=agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        conversation_id = await create_conversation(client)
        url = f"/v1/conversations/{conversation_id}/messages/stream"
        headers = {"Idempotency-Key": "stream-key-1"}
        first = await client.post(
            url,
            json={"content": "計算して"},
            headers=headers,
        )
        replay = await client.post(
            url,
            json={"content": "計算して"},
            headers=headers,
        )

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    assert "event: message.started" in first.text
    assert "event: assistant.delta" in first.text
    assert "event: tool.started" in first.text
    assert "event: tool.completed" in first.text
    assert "event: message.completed" in first.text
    assert '"content":"答えは2です。"' in first.text
    assert replay.text == first.text
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert agent.stream_calls == 1


@pytest.mark.asyncio
async def test_sse_agent_error_emits_failed_event_and_releases_lock():
    class FailingStreamAgent(StubAgentService):
        async def astream_events(self, *_args, **_kwargs):
            raise AgentTimeoutError() from RuntimeError("internal timeout detail")
            yield  # pragma: no cover

    registry = ConversationExecutionRegistry()
    app = make_test_app(
        agent_service=FailingStreamAgent(),
        execution_registry=registry,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        conversation_id = await create_conversation(client)
        response = await client.post(
            f"/v1/conversations/{conversation_id}/messages/stream",
            json={"content": "質問"},
        )

    assert response.status_code == 200
    assert "event: message.failed" in response.text
    assert '"code":"agent_timeout"' in response.text
    assert "internal timeout detail" not in response.text
    assert await registry.reserve(uuid.UUID(conversation_id)) is True


@pytest.mark.asyncio
async def test_same_conversation_rejects_overlapping_messages():
    class BlockingAgent(StubAgentService):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def ainvoke(self, prompt: str, thread_id: str) -> AgentRunResult:
            self.started.set()
            await self.release.wait()
            return await super().ainvoke(prompt, thread_id)

    agent = BlockingAgent()
    app = make_test_app(agent_service=agent)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        conversation = await client.post("/v1/conversations")
        conversation_id = conversation.json()["id"]
        url = f"/v1/conversations/{conversation_id}/messages"
        first_task = asyncio.create_task(
            client.post(url, json={"content": "最初の質問"})
        )
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        second = await client.post(url, json={"content": "次の質問"})
        agent.release.set()
        first = await asyncio.wait_for(first_task, timeout=1)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conversation_busy"


@pytest.mark.asyncio
async def test_disconnected_sse_releases_conversation_reservation():
    agent = StubAgentService()
    store = InMemoryConversationStore()
    registry = ConversationExecutionRegistry()
    app = make_test_app(
        agent_service=agent,
        conversation_store=store,
        execution_registry=registry,
    )
    record = await store.create()

    async def receive():
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/v1/conversations/{record.id}/messages/stream",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "scheme": "http",
        },
        receive=receive,
    )
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None)
        == "/v1/conversations/{conversation_id}/messages/stream"
    )
    response = await route.endpoint(
        conversation_id=record.id,
        message=MessageRequest(content="質問"),
        request=request,
        idempotency_key=None,
    )
    chunks = [chunk async for chunk in response.body_iterator]
    reserved_again = await registry.reserve(record.id)

    assert chunks == []
    assert reserved_again is True
    assert agent.stream_calls == 0
