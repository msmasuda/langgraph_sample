"""Unit tests for LangGraph agent structure and flow."""

from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage
from src.agent import create_agent
from src.state import AgentState


def test_agent_compilation():
    """Test that create_agent successfully compiles the StateGraph."""
    agent = create_agent(model_name="qwen3.5:9b-mlx")
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

        agent = create_agent(model_name="mock-model")
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

        agent = create_agent(model_name="mock-model")
        config = {"configurable": {"thread_id": "test-thread"}}
        response = agent.invoke({"messages": [HumanMessage(content="こんにちは")]}, config=config)

        assert len(response["messages"]) >= 2
        assert response["messages"][-1].content == "こんにちは！お手伝いできることはありますか？"
