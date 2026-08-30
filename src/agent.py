"""LangGraph Agent construction with Ollama and tools."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool
from langchain_ollama import ChatOllama
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from src.config import Settings, get_settings
from src.state import AgentState
from src.tools import ALL_TOOLS

# Persistent checkpoint storage
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "checkpoints.sqlite"


class AsyncCompatibleSqliteSaver(SqliteSaver):
    """Run SQLite checkpointer operations in worker threads for async graphs."""

    async def aget_tuple(self, config: Any) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: Any | None,
        *,
        filter: dict[str, Any] | None = None,
        before: Any | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        checkpoints = await asyncio.to_thread(
            lambda: list(
                self.list(
                    config,
                    filter=filter,
                    before=before,
                    limit=limit,
                )
            )
        )
        for checkpoint in checkpoints:
            yield checkpoint

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        return await asyncio.to_thread(
            self.put,
            config,
            checkpoint,
            metadata,
            new_versions,
        )

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(
            self.put_writes,
            config,
            writes,
            task_id,
            task_path,
        )

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


@lru_cache(maxsize=1)
def get_default_checkpointer() -> AsyncCompatibleSqliteSaver:
    """Return the process-wide persistent SQLite checkpointer."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    return AsyncCompatibleSqliteSaver(conn)


def create_agent(
    model_name: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    system_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    tools: list[BaseTool] | None = None,
    chat_model: BaseChatModel | None = None,
    settings: Settings | None = None,
):
    """Build and compile the LangGraph ReAct agent.

    Args:
        model_name: Ollama model name (e.g. 'qwen3.5:9b-mlx').
        base_url: Ollama API base URL.
        temperature: Sampling temperature.
        system_prompt: System prompt instructing the agent behavior.
        checkpointer: Checkpointer instance for thread memory. Defaults to persistent SqliteSaver.
        tools: List of tools to provide to the agent. Default: ALL_TOOLS.
        chat_model: Optional preconfigured model for dependency injection and tests.
        settings: Explicit application settings. Defaults to cached environment settings.

    Returns:
        Compiled LangGraph Pregel application.
    """
    active_settings = settings or get_settings()

    active_model = model_name or active_settings.ollama_model
    active_base_url = base_url or active_settings.ollama_base_url
    active_temp = temperature if temperature is not None else active_settings.temperature
    active_sys_prompt = active_settings.system_prompt if system_prompt is None else system_prompt
    active_tools = tools if tools is not None else ALL_TOOLS

    # Initialize Ollama LLM
    llm = chat_model or ChatOllama(
        model=active_model,
        base_url=active_base_url,
        temperature=active_temp,
        client_kwargs={"timeout": active_settings.ollama_request_timeout_seconds},
    )

    # Bind tools to the model
    llm_with_tools = llm.bind_tools(active_tools)

    def prepare_prompt(state: AgentState) -> tuple[list[BaseMessage], list[BaseMessage]]:
        """Trim persisted history and prepend the active system prompt."""
        all_messages = list(state["messages"])
        messages = trim_messages(
            all_messages,
            max_tokens=active_settings.max_context_tokens,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            include_system=False,
        )
        if not messages or not isinstance(messages[0], SystemMessage):
            prompt_messages = [SystemMessage(content=active_sys_prompt)] + messages
        else:
            prompt_messages = messages
        return messages, prompt_messages

    def apply_empty_response_fallback(
        response: BaseMessage,
        messages: list[BaseMessage],
        prompt_messages: list[BaseMessage],
    ) -> BaseMessage:
        """Guarantee a user-visible response after synchronous model execution."""
        if getattr(response, "tool_calls", None) or str(
            getattr(response, "content", "")
        ).strip():
            return response

        if messages and (
            isinstance(messages[-1], ToolMessage)
            or getattr(messages[-1], "type", None) == "tool"
        ):
            summary_prompt = prompt_messages + [
                HumanMessage(
                    content=(
                        "ツールの実行結果をもとに、ユーザーの質問に対する回答を"
                        "日本語で分かりやすくまとめて出力してください。"
                    )
                )
            ]
            try:
                retry_response = llm.invoke(summary_prompt)
                if str(getattr(retry_response, "content", "")).strip():
                    return retry_response
            except Exception:
                pass
            response.content = (
                f"【ツール実行結果】\n{getattr(messages[-1], 'content', '')}"
            )
        else:
            response.content = (
                "申し訳ありません。回答の生成に失敗しました。"
                "もう一度質問を入力してください。"
            )
        return response

    async def apply_empty_response_fallback_async(
        response: BaseMessage,
        messages: list[BaseMessage],
        prompt_messages: list[BaseMessage],
    ) -> BaseMessage:
        """Guarantee a user-visible response after asynchronous model execution."""
        if getattr(response, "tool_calls", None) or str(
            getattr(response, "content", "")
        ).strip():
            return response

        if messages and (
            isinstance(messages[-1], ToolMessage)
            or getattr(messages[-1], "type", None) == "tool"
        ):
            summary_prompt = prompt_messages + [
                HumanMessage(
                    content=(
                        "ツールの実行結果をもとに、ユーザーの質問に対する回答を"
                        "日本語で分かりやすくまとめて出力してください。"
                    )
                )
            ]
            try:
                retry_response = await llm.ainvoke(summary_prompt)
                if str(getattr(retry_response, "content", "")).strip():
                    return retry_response
            except Exception:
                pass
            response.content = (
                f"【ツール実行結果】\n{getattr(messages[-1], 'content', '')}"
            )
        else:
            response.content = (
                "申し訳ありません。回答の生成に失敗しました。"
                "もう一度質問を入力してください。"
            )
        return response

    # Define synchronous and asynchronous variants of the chatbot node.
    def chatbot_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        messages, prompt_messages = prepare_prompt(state)

        response = llm_with_tools.invoke(prompt_messages)
        response = apply_empty_response_fallback(response, messages, prompt_messages)
        return {"messages": [response]}

    async def chatbot_node_async(state: AgentState) -> dict[str, list[BaseMessage]]:
        messages, prompt_messages = prepare_prompt(state)
        response = await llm_with_tools.ainvoke(prompt_messages)
        response = await apply_empty_response_fallback_async(
            response,
            messages,
            prompt_messages,
        )
        return {"messages": [response]}

    # Build the StateGraph
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node(
        "chatbot",
        RunnableLambda(chatbot_node, afunc=chatbot_node_async),
    )
    workflow.add_node("tools", ToolNode(active_tools))

    # Add edges
    workflow.add_edge(START, "chatbot")
    workflow.add_conditional_edges(
        "chatbot",
        tools_condition,
    )
    workflow.add_edge("tools", "chatbot")

    # Use persistent SqliteSaver checkpointer if none specified
    if checkpointer is None:
        checkpointer = get_default_checkpointer()

    # Compile the graph
    app = workflow.compile(checkpointer=checkpointer)
    return app
