"""Streamlit Web UI for LangGraph Ollama Agent."""

import uuid

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from src.agent import create_agent, get_default_checkpointer
from src.config import get_settings
from src.state import AgentState
from src.tools import ALL_TOOLS, read_notes

# Page configuration
st.set_page_config(
    page_title="LangGraph Ollama Agent",
    page_icon="🤖",
    layout="wide",
)

settings = get_settings()

# Initialize session state. The unguessable ID in the URL acts as the local
# conversation key so a browser reload can restore the same checkpoint.
if "thread_id" not in st.session_state:
    query_thread = st.query_params.get("thread")
    try:
        parsed_thread = uuid.UUID(query_thread) if query_thread else None
    except (TypeError, ValueError):
        parsed_thread = None
    st.session_state.thread_id = str(parsed_thread or uuid.uuid4())
    st.query_params["thread"] = st.session_state.thread_id

if "messages" not in st.session_state:
    st.session_state.messages = []

# Persistent checkpointer for Streamlit session
if "checkpointer" not in st.session_state:
    st.session_state.checkpointer = get_default_checkpointer()

# Sidebar configuration
with st.sidebar:
    st.title("⚙️ 設定 & 管理")

    model_options = [
        "qwen3.5:9b-mlx",
        "qwen3.8:27b-mlx",
        "gemma4:12b-mlx",
        "ornith-1.5:9b",
    ]
    default_index = 0
    if settings.ollama_model in model_options:
        default_index = model_options.index(settings.ollama_model)

    selected_model = st.selectbox(
        "🤖 Ollama モデル",
        options=model_options,
        index=default_index,
    )
    custom_model = st.text_input("または直接モデル名を入力", placeholder="例: llama3.3:70b")
    active_model = custom_model.strip() if custom_model.strip() else selected_model

    st.caption(f"Ollama接続先: `{settings.ollama_base_url}`")
    temperature = st.slider("🌡️ Temperature", min_value=0.0, max_value=1.0, value=settings.temperature, step=0.05)

    st.divider()

    st.subheader("💬 セッション管理")
    st.code(st.session_state.thread_id, language=None)

    if st.button("🔄 新しい会話を開始", width="stretch"):
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.messages = []
        st.query_params["thread"] = st.session_state.thread_id
        st.rerun()

    st.divider()

    with st.expander("🛠️ 利用可能なツール一覧", expanded=False):
        for tool in ALL_TOOLS:
            st.markdown(f"**`{tool.name}`**")
            st.caption(tool.description)

    with st.expander("📝 保存されたメモの確認", expanded=False):
        notes_config: RunnableConfig = {
            "configurable": {"thread_id": st.session_state.thread_id}
        }
        notes_str = read_notes.invoke({}, config=notes_config)
        st.text(notes_str)

# Main UI Header
st.title("🤖 LangGraph + Ollama 自律型エージェント")
st.caption(f"スレッドID: `{st.session_state.thread_id}` | LangGraphの永続メモリ（SQLite）により、ブラウザを再読み込みしても会話コンテキストが維持されます。")

# Sync messages from checkpointer if session_state messages is empty
agent_key = (active_model, settings.ollama_base_url, temperature)
if st.session_state.get("agent_key") != agent_key:
    st.session_state.agent = create_agent(
        model_name=active_model,
        base_url=settings.ollama_base_url,
        temperature=temperature,
        checkpointer=st.session_state.checkpointer,
    )
    st.session_state.agent_key = agent_key
agent = st.session_state.agent
config: RunnableConfig = {
    "configurable": {"thread_id": st.session_state.thread_id},
    "recursion_limit": 15,
}

if not st.session_state.messages:
    try:
        saved_state = agent.get_state(config)
        if saved_state and saved_state.values and "messages" in saved_state.values:
            history = []
            for msg in saved_state.values["messages"]:
                if isinstance(msg, HumanMessage):
                    history.append({"role": "user", "content": msg.content})
                elif isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
                    history.append({"role": "assistant", "content": msg.content, "tool_events": []})
            st.session_state.messages = history
    except Exception as error:
        st.warning(f"保存済み履歴を読み込めませんでした: {error}")

# Display message history
for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    elif msg["role"] == "assistant":
        with st.chat_message("assistant"):
            if "tool_events" in msg and msg["tool_events"]:
                with st.expander(f"⚙️ ツール実行ログ ({len(msg['tool_events'])}件)", expanded=False):
                    for event in msg["tool_events"]:
                        st.markdown(f"**ツール:** `{event.get('name')}`")
                        if "args" in event:
                            st.json(event["args"])
                        if "output" in event:
                            st.code(event["output"], language="text")
            st.markdown(msg["content"])

# Chat input
if prompt := st.chat_input("質問や指示を入力してください...", submit_mode="disable"):
    # Append user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        tool_events = []
        tool_events_by_id = {}
        final_response = ""
        execution_error = None

        with st.status("エージェントが思考・ツール実行中...", expanded=True) as status:
            inputs: AgentState = {"messages": [HumanMessage(content=prompt)]}

            try:
                for chunk in agent.stream(inputs, config, stream_mode="updates"):
                    for node_name, node_output in chunk.items():
                        messages = node_output.get("messages", [])
                        for m in messages:
                            # Tool calls requested by LLM
                            tool_calls = getattr(m, "tool_calls", None)
                            if tool_calls:
                                for tc in tool_calls:
                                    t_name = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                                    t_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                                    call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                                    st.write(f"⚙️ ツール呼び出し: **`{t_name}`**")
                                    event = {"name": t_name, "args": t_args}
                                    tool_events.append(event)
                                    if call_id:
                                        tool_events_by_id[call_id] = event
                            elif isinstance(m, ToolMessage) or getattr(m, "type", None) == "tool":
                                tool_name = getattr(m, "name", "tool")
                                content = getattr(m, "content", "")
                                call_id = getattr(m, "tool_call_id", None)
                                st.write(f"📥 ツール実行完了: **`{tool_name}`**")
                                event = tool_events_by_id.get(call_id)
                                if event is None:
                                    event = {"name": tool_name}
                                    tool_events.append(event)
                                event["output"] = str(content)
                            elif (isinstance(m, AIMessage) or getattr(m, "type", None) == "ai") and not tool_calls:
                                final_response = getattr(m, "content", "")

                status.update(label="完了しました！", state="complete", expanded=False)
            except Exception as error:
                execution_error = error
                status.update(label="エラーが発生しました", state="error", expanded=True)
                st.error(f"実行エラー: {error}")

        if execution_error is not None:
            error_message = "回答を生成できませんでした。Ollamaの起動状態と設定を確認してください。"
            st.session_state.messages.append({
                "role": "assistant",
                "content": error_message,
                "tool_events": tool_events,
            })
        elif final_response:
            st.markdown(final_response)
            st.session_state.messages.append({
                "role": "assistant",
                "content": final_response,
                "tool_events": tool_events,
            })
        else:
            fallback_msg = "回答を生成できませんでした。もう一度お試しください。"
            st.markdown(fallback_msg)
            st.session_state.messages.append({
                "role": "assistant",
                "content": fallback_msg,
                "tool_events": tool_events,
            })
