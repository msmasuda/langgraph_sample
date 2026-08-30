"""LangGraph Agent construction with Ollama and tools."""

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.tools import BaseTool
from langchain_ollama import ChatOllama
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from src.config import get_settings
from src.state import AgentState
from src.tools import ALL_TOOLS

# Persistent checkpoint storage
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "checkpoints.sqlite"


@lru_cache(maxsize=1)
def get_default_checkpointer() -> SqliteSaver:
    """Return the process-wide persistent SQLite checkpointer."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    return SqliteSaver(conn)


def create_agent(
    model_name: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    system_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    tools: list[BaseTool] | None = None,
):
    """Build and compile the LangGraph ReAct agent.

    Args:
        model_name: Ollama model name (e.g. 'qwen3.5:9b-mlx').
        base_url: Ollama API base URL.
        temperature: Sampling temperature.
        system_prompt: System prompt instructing the agent behavior.
        checkpointer: Checkpointer instance for thread memory. Defaults to persistent SqliteSaver.
        tools: List of tools to provide to the agent. Default: ALL_TOOLS.

    Returns:
        Compiled LangGraph Pregel application.
    """
    settings = get_settings()

    active_model = model_name or settings.ollama_model
    active_base_url = base_url or settings.ollama_base_url
    active_temp = temperature if temperature is not None else settings.temperature
    active_sys_prompt = settings.system_prompt if system_prompt is None else system_prompt
    active_tools = tools if tools is not None else ALL_TOOLS

    # Initialize Ollama LLM
    llm = ChatOllama(
        model=active_model,
        base_url=active_base_url,
        temperature=active_temp,
    )

    # Bind tools to the model
    llm_with_tools = llm.bind_tools(active_tools)

    # Define the agent/chatbot node
    def chatbot_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        all_messages = list(state["messages"])
        messages = trim_messages(
            all_messages,
            max_tokens=settings.max_context_tokens,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            include_system=False,
        )

        # Prepend system message if not already present in the prompt to the LLM
        prompt_messages: list[BaseMessage] = []
        if not messages or not isinstance(messages[0], SystemMessage):
            prompt_messages = [SystemMessage(content=active_sys_prompt)] + messages
        else:
            prompt_messages = messages

        response = llm_with_tools.invoke(prompt_messages)

        # Safety fallback: if LLM returned empty content and no tool calls
        if not getattr(response, "tool_calls", None) and not str(getattr(response, "content", "")).strip():
            if messages and (isinstance(messages[-1], ToolMessage) or getattr(messages[-1], "type", None) == "tool"):
                summary_prompt = prompt_messages + [
                    HumanMessage(content="ツールの実行結果をもとに、ユーザーの質問に対する回答を日本語で分かりやすくまとめて出力してください。")
                ]
                try:
                    retry_response = llm.invoke(summary_prompt)
                    if str(getattr(retry_response, "content", "")).strip():
                        return {"messages": [retry_response]}
                except Exception:
                    pass
                response.content = f"【ツール実行結果】\n{getattr(messages[-1], 'content', '')}"
            else:
                response.content = "申し訳ありません。回答の生成に失敗しました。もう一度質問を入力してください。"

        return {"messages": [response]}

    # Build the StateGraph
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("chatbot", chatbot_node)
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
