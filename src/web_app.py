"""Streamlit Web UI for LangGraph Ollama Agent."""

import uuid

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from src.agent import get_default_checkpointer
from src.config import get_settings
from src.errors import AgentServiceError
from src.services import AgentService, OllamaModelService, OllamaStatus
from src.tools import ALL_TOOLS, read_notes

# Page configuration
st.set_page_config(
    page_title="LangGraph Ollama Agent",
    page_icon="🤖",
    layout="wide",
)

settings = get_settings()


@st.cache_data(ttl=30, max_entries=10, show_spinner=False)
def get_ollama_status(base_url: str, timeout_seconds: float) -> OllamaStatus:
    """Return a short-lived cached Ollama health and model result."""
    return OllamaModelService(base_url, timeout_seconds).get_status()


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

    fallback_model_options = [
        "qwen3.5:9b-mlx",
        "qwen3.8:27b-mlx",
        "gemma4:12b-mlx",
        "ornith-1.5:9b",
    ]
    ollama_status = get_ollama_status(
        settings.ollama_base_url,
        settings.ollama_health_timeout_seconds,
    )
    model_options = list(ollama_status.models) or fallback_model_options
    if settings.ollama_model not in model_options:
        model_options.insert(0, settings.ollama_model)
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

    if ollama_status.available:
        st.success("Ollamaに接続済みです。", icon=":material/check_circle:")
    else:
        st.warning("Ollamaに接続できません。", icon=":material/warning:")
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
    try:
        st.session_state.agent_service = AgentService.create(
            model_name=active_model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
            checkpointer=st.session_state.checkpointer,
            settings=settings,
        )
    except AgentServiceError as error:
        st.error(error.user_message)
        st.stop()
    st.session_state.agent_key = agent_key
agent_service = st.session_state.agent_service

if not st.session_state.messages:
    try:
        saved_state = agent_service.get_state(st.session_state.thread_id)
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
            try:
                for event in agent_service.stream_events(
                    prompt,
                    st.session_state.thread_id,
                ):
                    if event.type == "tool_started":
                        st.write(f"⚙️ ツール呼び出し: **`{event.tool_name}`**")
                        tool_event = {
                            "name": event.tool_name,
                            "args": dict(event.tool_args),
                        }
                        tool_events.append(tool_event)
                        if event.tool_call_id:
                            tool_events_by_id[event.tool_call_id] = tool_event
                    elif event.type == "tool_completed":
                        st.write(f"📥 ツール実行完了: **`{event.tool_name}`**")
                        tool_event = tool_events_by_id.get(event.tool_call_id)
                        if tool_event is None:
                            tool_event = {"name": event.tool_name}
                            tool_events.append(tool_event)
                        tool_event["output"] = event.content
                    elif event.type == "assistant_completed":
                        final_response = event.content
                status.update(label="完了しました！", state="complete", expanded=False)
            except AgentServiceError as error:
                execution_error = error
                status.update(label="エラーが発生しました", state="error", expanded=True)
                st.error(error.user_message)
            except Exception:
                execution_error = True
                status.update(label="エラーが発生しました", state="error", expanded=True)
                st.error("予期しないエラーが発生しました。")

        if execution_error is not None:
            error_message = "回答を生成できませんでした。設定を確認してもう一度お試しください。"
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
