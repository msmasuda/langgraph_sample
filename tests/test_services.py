"""Tests for shared agent and Ollama services."""

import asyncio
import io
from unittest.mock import patch
from urllib.error import URLError

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from src.errors import AgentConnectionError, AgentLimitError, AgentTimeoutError
from src.services import AgentService, OllamaModelService


class StubGraph:
    """Small graph stub that emits configured update chunks."""

    def __init__(self, chunks, delay: float = 0.0):
        self.chunks = chunks
        self.delay = delay

    async def astream(self, *_args, **_kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        for chunk in self.chunks:
            yield chunk

    def stream(self, *_args, **_kwargs):
        yield from self.chunks


def make_service(graph, *, max_tool_calls=8, timeout_seconds=1.0):
    return AgentService(
        graph,
        recursion_limit=15,
        max_tool_calls=max_tool_calls,
        timeout_seconds=timeout_seconds,
    )


def test_agent_service_async_result():
    """Collect a normalized assistant response from async graph updates."""
    graph = StubGraph(
        [{"chatbot": {"messages": [AIMessage(content="完了しました。")]}}]
    )

    result = asyncio.run(make_service(graph).ainvoke("質問", "thread-1"))

    assert result.response == "完了しました。"
    assert result.events[-1].type == "assistant_completed"


def test_agent_service_streams_token_events():
    """Normalize LangGraph messages-mode chunks as assistant deltas."""

    class TokenGraph:
        async def astream(self, *_args, **_kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(content="途中"),
                    {"langgraph_node": "chatbot"},
                ),
            )
            yield (
                "updates",
                {"chatbot": {"messages": [AIMessage(content="途中です。")]}},
            )

    async def run():
        return [
            event
            async for event in make_service(TokenGraph()).astream_events(
                "質問",
                "thread-1",
                include_tokens=True,
            )
        ]

    events = asyncio.run(run())

    assert [event.type for event in events] == [
        "assistant_delta",
        "assistant_completed",
    ]
    assert events[0].content == "途中"


def test_agent_service_stops_before_excess_tool_execution():
    """Reject a model response that exceeds the configured tool-call budget."""
    calls = [
        {"name": "calculator", "args": {"expression": str(index)}, "id": str(index)}
        for index in range(2)
    ]
    graph = StubGraph(
        [{"chatbot": {"messages": [AIMessage(content="", tool_calls=calls)]}}]
    )

    async def run():
        return [
            event
            async for event in make_service(
                graph,
                max_tool_calls=1,
            ).astream_events("計算", "thread-1")
        ]

    with pytest.raises(AgentLimitError):
        asyncio.run(run())


def test_agent_service_async_timeout():
    """Convert an overall async execution timeout to a safe service error."""
    graph = StubGraph([], delay=0.05)

    with pytest.raises(AgentTimeoutError) as captured:
        asyncio.run(
            make_service(graph, timeout_seconds=0.01).ainvoke("質問", "thread-1")
        )

    assert "タイムアウト" in captured.value.user_message


def test_agent_service_hides_connection_details():
    """Convert low-level HTTP errors without exposing their internal message."""

    class FailingGraph:
        async def astream(self, *_args, **_kwargs):
            raise httpx.ConnectError("sensitive internal host")
            yield  # pragma: no cover

    with pytest.raises(AgentConnectionError) as captured:
        asyncio.run(make_service(FailingGraph()).ainvoke("質問", "thread-1"))

    assert "sensitive internal host" not in str(captured.value)


def test_agent_service_converts_initialization_error():
    """Convert graph construction errors before they reach a UI."""
    with patch(
        "src.services.agent_service.create_agent",
        side_effect=httpx.ConnectError("sensitive initialization detail"),
    ):
        with pytest.raises(AgentConnectionError) as captured:
            AgentService.create()

    assert "sensitive initialization detail" not in str(captured.value)


def test_ollama_model_service_lists_models():
    """Parse and sort installed model names from the Ollama tags endpoint."""
    response = io.BytesIO(
        b'{"models":[{"name":"qwen:latest"},{"model":"gemma:latest"}]}'
    )

    with patch("src.services.model_service.urlopen", return_value=response) as urlopen:
        models = OllamaModelService("http://localhost:11434", 2.0).list_models()

    assert models == ("gemma:latest", "qwen:latest")
    assert urlopen.call_args.kwargs["timeout"] == 2.0


def test_ollama_model_service_returns_safe_unavailable_status():
    """Hide low-level connection errors from status consumers."""
    with patch(
        "src.services.model_service.urlopen",
        side_effect=URLError("internal connection detail"),
    ):
        status = OllamaModelService("http://localhost:11434").get_status()

    assert status.available is False
    assert status.models == ()
