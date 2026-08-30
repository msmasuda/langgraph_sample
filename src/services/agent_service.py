"""Shared service for running and observing the LangGraph agent."""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from src.agent import create_agent
from src.config import Settings, get_settings
from src.errors import (
    AgentConnectionError,
    AgentExecutionError,
    AgentLimitError,
    AgentServiceError,
    AgentTimeoutError,
)
from src.state import AgentState

AgentEventType = Literal[
    "tool_started",
    "tool_completed",
    "assistant_delta",
    "assistant_completed",
]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """UI-agnostic event emitted while the agent is running."""

    type: AgentEventType
    node_name: str
    content: str = ""
    tool_name: str | None = None
    tool_args: Mapping[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Completed agent response and all execution events."""

    response: str
    events: tuple[AgentEvent, ...]


class AgentService:
    """Run a compiled LangGraph agent behind a stable application interface."""

    def __init__(
        self,
        graph: Any,
        *,
        recursion_limit: int,
        max_tool_calls: int,
        timeout_seconds: float,
    ) -> None:
        self.graph = graph
        self.recursion_limit = recursion_limit
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds

    @classmethod
    def create(
        cls,
        *,
        model_name: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        system_prompt: str | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        tools: list[BaseTool] | None = None,
        chat_model: BaseChatModel | None = None,
        settings: Settings | None = None,
    ) -> "AgentService":
        """Create the graph and service from application settings."""
        active_settings = settings or get_settings()
        try:
            graph = create_agent(
                model_name=model_name,
                base_url=base_url,
                temperature=temperature,
                system_prompt=system_prompt,
                checkpointer=checkpointer,
                tools=tools,
                chat_model=chat_model,
                settings=active_settings,
            )
        except Exception as error:
            raise cls._convert_error(error) from error
        return cls(
            graph,
            recursion_limit=active_settings.recursion_limit,
            max_tool_calls=active_settings.max_tool_calls,
            timeout_seconds=active_settings.agent_timeout_seconds,
        )

    def _config(self, thread_id: str, user_id: str | None = None) -> RunnableConfig:
        normalized_thread_id = thread_id.strip()
        if not normalized_thread_id or len(normalized_thread_id) > 200:
            raise ValueError("有効なスレッドIDが必要です")
        configurable = {"thread_id": normalized_thread_id}
        if user_id is not None:
            configurable["user_id"] = str(user_id)
        return {
            "configurable": configurable,
            "recursion_limit": self.recursion_limit,
        }

    @staticmethod
    def _events_from_chunk(chunk: Any) -> Iterator[AgentEvent]:
        if not isinstance(chunk, dict):
            return

        for node_name, node_output in chunk.items():
            if not isinstance(node_output, dict):
                continue
            for message in node_output.get("messages", []):
                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    for tool_call in tool_calls:
                        if isinstance(tool_call, dict):
                            tool_name = str(tool_call.get("name", "unknown"))
                            tool_args = tool_call.get("args", {})
                            call_id = tool_call.get("id")
                        else:
                            tool_name = str(getattr(tool_call, "name", "unknown"))
                            tool_args = getattr(tool_call, "args", {})
                            call_id = getattr(tool_call, "id", None)
                        yield AgentEvent(
                            type="tool_started",
                            node_name=node_name,
                            tool_name=tool_name,
                            tool_args=tool_args if isinstance(tool_args, Mapping) else {},
                            tool_call_id=str(call_id) if call_id else None,
                        )
                elif isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
                    call_id = getattr(message, "tool_call_id", None)
                    yield AgentEvent(
                        type="tool_completed",
                        node_name=node_name,
                        content=str(getattr(message, "content", "")),
                        tool_name=str(getattr(message, "name", "tool")),
                        tool_call_id=str(call_id) if call_id else None,
                    )
                elif isinstance(message, AIMessage) or getattr(message, "type", None) == "ai":
                    yield AgentEvent(
                        type="assistant_completed",
                        node_name=node_name,
                        content=str(getattr(message, "content", "")),
                    )

    @staticmethod
    def _content_text(content: Any) -> str:
        """Normalize text and text content blocks emitted by chat models."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _convert_error(error: Exception) -> AgentServiceError:
        if isinstance(error, AgentServiceError):
            return error
        if isinstance(error, (TimeoutError, httpx.TimeoutException)):
            return AgentTimeoutError()
        if isinstance(error, httpx.HTTPError):
            return AgentConnectionError()
        return AgentExecutionError()

    def stream_events(
        self,
        prompt: str,
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> Iterator[AgentEvent]:
        """Synchronously stream normalized events for compatibility clients."""
        inputs: AgentState = {"messages": [HumanMessage(content=prompt)]}
        deadline = time.monotonic() + self.timeout_seconds
        tool_call_count = 0

        try:
            for chunk in self.graph.stream(
                inputs,
                self._config(thread_id, user_id),
                stream_mode="updates",
            ):
                if time.monotonic() > deadline:
                    raise AgentTimeoutError()
                for event in self._events_from_chunk(chunk):
                    if event.type == "tool_started":
                        tool_call_count += 1
                        if tool_call_count > self.max_tool_calls:
                            raise AgentLimitError()
                    yield event
        except Exception as error:
            converted = self._convert_error(error)
            if converted is error:
                raise
            raise converted from error

    async def astream_events(
        self,
        prompt: str,
        thread_id: str,
        *,
        include_tokens: bool = False,
        user_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Asynchronously stream normalized events with an overall timeout."""
        inputs: AgentState = {"messages": [HumanMessage(content=prompt)]}
        tool_call_count = 0
        stream_mode: str | list[str] = (
            ["messages", "updates"] if include_tokens else "updates"
        )

        try:
            async with asyncio.timeout(self.timeout_seconds):
                async for streamed in self.graph.astream(
                    inputs,
                    self._config(thread_id, user_id),
                    stream_mode=stream_mode,
                ):
                    if include_tokens:
                        mode, data = streamed
                        if mode == "messages":
                            message, metadata = data
                            if isinstance(message, AIMessageChunk):
                                content = self._content_text(message.content)
                                if content:
                                    node_name = (
                                        str(metadata.get("langgraph_node", "chatbot"))
                                        if isinstance(metadata, dict)
                                        else "chatbot"
                                    )
                                    yield AgentEvent(
                                        type="assistant_delta",
                                        node_name=node_name,
                                        content=content,
                                    )
                            continue
                        if mode != "updates":
                            continue
                        chunk = data
                    else:
                        chunk = streamed

                    for event in self._events_from_chunk(chunk):
                        if event.type == "tool_started":
                            tool_call_count += 1
                            if tool_call_count > self.max_tool_calls:
                                raise AgentLimitError()
                        yield event
        except Exception as error:
            converted = self._convert_error(error)
            if converted is error:
                raise
            raise converted from error

    def invoke(
        self,
        prompt: str,
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> AgentRunResult:
        """Synchronously execute the agent and collect its events."""
        events = tuple(self.stream_events(prompt, thread_id, user_id=user_id))
        response = next(
            (event.content for event in reversed(events) if event.type == "assistant_completed"),
            "",
        )
        return AgentRunResult(response=response, events=events)

    async def ainvoke(
        self,
        prompt: str,
        thread_id: str,
        *,
        user_id: str | None = None,
    ) -> AgentRunResult:
        """Asynchronously execute the agent and collect its events."""
        events = tuple(
            [
                event
                async for event in self.astream_events(
                    prompt,
                    thread_id,
                    user_id=user_id,
                )
            ]
        )
        response = next(
            (event.content for event in reversed(events) if event.type == "assistant_completed"),
            "",
        )
        return AgentRunResult(response=response, events=events)

    def get_state(self, thread_id: str, *, user_id: str | None = None) -> Any:
        """Return the latest persisted state for a thread."""
        return self.graph.get_state(self._config(thread_id, user_id))

    async def aget_state(self, thread_id: str, *, user_id: str | None = None) -> Any:
        """Asynchronously return the latest persisted state for a thread."""
        return await self.graph.aget_state(self._config(thread_id, user_id))

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete all persisted checkpoints for a thread."""
        checkpointer = getattr(self.graph, "checkpointer", None)
        delete_thread = getattr(checkpointer, "adelete_thread", None)
        if delete_thread is None:
            raise AgentExecutionError()
        try:
            await delete_thread(self._config(thread_id)["configurable"]["thread_id"])
        except Exception as error:
            converted = self._convert_error(error)
            if converted is error:
                raise
            raise converted from error
