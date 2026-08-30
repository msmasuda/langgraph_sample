"""State definition for LangGraph agent."""

from typing import Annotated, Sequence
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """State schema for the agent graph.

    Attributes:
        messages: Sequence of chat messages, with reducer for appending new messages.
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]
