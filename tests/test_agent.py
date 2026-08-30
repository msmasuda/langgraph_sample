"""Unit tests for LangGraph agent structure and flow."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from src.agent import AsyncCompatibleSqliteSaver, create_agent
from src.state import AgentState


def test_agent_compilation():
    """Test that create_agent successfully compiles the StateGraph."""
    agent = create_agent(
        model_name="qwen3.5:9b-mlx",
        checkpointer=InMemorySaver(),
    )
    assert agent is not None
    # Verify graph nodes exist
    node_keys = list(agent.get_graph().nodes.keys())
    assert "chatbot" in node_keys
    assert "tools" in node_keys


def test_agent_empty_response_uses_fallback():
    """Test that an empty model response does not crash the graph."""
    with patch("src.agent.ChatOllama") as mock_chat_ollama:
        mock_llm = MagicMock()
        mock_chat_ollama.return_value = mock_llm
        mock_bound_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_bound_llm
        mock_bound_llm.invoke.return_value = AIMessage(content="")

        agent = create_agent(
            model_name="mock-model",
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "test-empty-response"}}
        response = agent.invoke(
            {"messages": [HumanMessage(content="こんにちは")]}, config=config
        )

        assert response["messages"][-1].content
        assert "回答の生成に失敗" in response["messages"][-1].content


def test_agent_flow_with_mock():
    """Test agent execution flow with mocked ChatOllama."""
    with patch("src.agent.ChatOllama") as mock_chat_ollama:
        mock_llm = MagicMock()
        mock_chat_ollama.return_value = mock_llm

        # Mock bind_tools
        mock_bound_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_bound_llm
        mock_bound_llm.invoke.return_value = AIMessage(content="こんにちは！お手伝いできることはありますか？")

        agent = create_agent(
            model_name="mock-model",
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "test-thread"}}
        response = agent.invoke({"messages": [HumanMessage(content="こんにちは")]}, config=config)

        assert len(response["messages"]) >= 2
        assert response["messages"][-1].content == "こんにちは！お手伝いできることはありますか？"


def test_agent_accepts_injected_chat_model():
    """Build the graph from an injected model without constructing ChatOllama."""
    mock_llm = MagicMock()
    mock_bound_llm = MagicMock()
    mock_bound_llm.invoke.return_value = AIMessage(content="注入されたモデルです。")
    mock_llm.bind_tools.return_value = mock_bound_llm

    with patch("src.agent.ChatOllama") as chat_ollama:
        agent = create_agent(
            chat_model=mock_llm,
            checkpointer=InMemorySaver(),
        )
        response = agent.invoke(
            {"messages": [HumanMessage(content="こんにちは")]},
            config={"configurable": {"thread_id": "test-injected-model"}},
        )

    chat_ollama.assert_not_called()
    assert response["messages"][-1].content == "注入されたモデルです。"


def test_agent_async_flow_with_persistent_checkpointer(tmp_path):
    """Test async graph execution with the SQLite compatibility saver."""
    connection = sqlite3.connect(
        tmp_path / "checkpoints.sqlite",
        check_same_thread=False,
    )
    checkpointer = AsyncCompatibleSqliteSaver(connection)

    with patch("src.agent.ChatOllama") as mock_chat_ollama:
        mock_llm = MagicMock()
        mock_chat_ollama.return_value = mock_llm
        mock_bound_llm = MagicMock()
        mock_bound_llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="非同期の回答です。")
        )
        mock_llm.bind_tools.return_value = mock_bound_llm

        agent = create_agent(model_name="mock-model", checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "test-async-thread"}}

        async def run_agent():
            response = await agent.ainvoke(
                {"messages": [HumanMessage(content="こんにちは")]},
                config=config,
            )
            state = await agent.aget_state(config)
            return response, state

        response, state = asyncio.run(run_agent())

    connection.close()
    assert response["messages"][-1].content == "非同期の回答です。"
    assert state.values["messages"][-1].content == "非同期の回答です。"
