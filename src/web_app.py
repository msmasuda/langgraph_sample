"""Streamlit chat UI backed exclusively by the shared FastAPI service."""

from __future__ import annotations

import time
import uuid
from typing import Any

import streamlit as st

from src.config import get_settings
from src.web_api_client import AgentApiClient, AgentApiError

st.set_page_config(
    page_title="LangGraph Ollama Agent",
    page_icon=":material/smart_toy:",
    layout="wide",
)

settings = get_settings()


@st.cache_data(ttl=5, max_entries=10, show_spinner=False)
def get_api_readiness(base_url: str, timeout_seconds: float) -> dict[str, Any]:
    """Return short-lived, non-sensitive API dependency status."""
    try:
        result = AgentApiClient(
            base_url,
            timeout_seconds=timeout_seconds,
        ).ready()
        return {"result": result.model_dump(), "error": None}
    except AgentApiError as error:
        return {"result": None, "error": error.user_message}
    except Exception:
        return {
            "result": None,
            "error": "APIの状態を取得できませんでした。",
        }


def streamlit_auth_error() -> str | None:
    """Validate only the presence of required Streamlit OIDC settings."""
    try:
        auth = st.secrets.get("auth", {})
    except Exception:
        return ".streamlit/secrets.tomlが設定されていません。"
    required = {
        "redirect_uri",
        "cookie_secret",
        "client_id",
        "client_secret",
        "server_metadata_url",
    }
    if not required.issubset(auth):
        return ".streamlit/secrets.tomlのOIDC設定が不足しています。"
    exposed = auth.get("expose_tokens")
    exposed_tokens = {exposed} if isinstance(exposed, str) else set(exposed or [])
    if "access" not in exposed_tokens:
        return "Streamlit認証設定でアクセストークンを有効にしてください。"
    return None


def show_api_error(error: AgentApiError) -> None:
    """Render only the API's safe message and correlation identifier."""
    message = error.user_message
    if error.retry_after:
        message += f"（約{error.retry_after}秒後に再試行できます）"
    st.error(message)
    if error.request_id:
        st.caption(f"問い合わせ用リクエストID: `{error.request_id}`")


def set_next_conversation(conversation_id: str | None) -> None:
    """Select a conversation before the selector widget is created."""
    st.session_state.next_conversation_id = conversation_id


st.title("LangGraph + Ollama エージェント")
st.caption("会話・履歴・メモは共通APIを経由して安全に保存されます。")

readiness = get_api_readiness(
    settings.web_api_base_url,
    settings.web_api_timeout_seconds,
)

with st.sidebar:
    st.header("接続状態")
    if readiness["error"]:
        st.error("API: 接続できません", icon=":material/error:")
        st.caption(readiness["error"])
    else:
        ready = readiness["result"]
        st.success("API: 接続済み", icon=":material/check_circle:")
        if ready["ollama"]:
            st.success("Ollama: 利用可能", icon=":material/check_circle:")
        else:
            st.error("Ollama: 利用できません", icon=":material/error:")
        if ready["database"] is True:
            st.success("PostgreSQL: 利用可能", icon=":material/check_circle:")
        elif ready["database"] is False:
            st.error("PostgreSQL: 利用できません", icon=":material/error:")
        else:
            st.info("PostgreSQL: 未使用", icon=":material/info:")
    st.caption(f"API接続先: `{settings.web_api_base_url}`")

access_token: str | None = None
if settings.auth_mode == "oidc":
    configuration_error = streamlit_auth_error()
    if configuration_error:
        st.error(configuration_error)
        st.info(
            "`.streamlit/secrets.toml.example`をコピーし、"
            "Keycloakの`langgraph-streamlit`クライアント情報を設定してください。"
        )
        st.stop()
    if not getattr(st.user, "is_logged_in", False):
        st.info("Keycloakでログインすると会話を利用できます。")
        if st.button("ログイン", type="primary", icon=":material/login:"):
            st.login()
        st.stop()
    expires_at = st.user.get("exp")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        st.warning("ログインの有効期限が切れました。再ログインしてください。")
        if st.button("再ログイン", icon=":material/login:"):
            st.logout()
        st.stop()
    access_token = st.user.tokens.get("access")
    if not access_token:
        st.error("アクセストークンを取得できません。認証設定を確認してください。")
        st.stop()
    with st.sidebar:
        st.divider()
        st.subheader("認証")
        display_name = st.user.get("name") or st.user.get("preferred_username")
        st.success("Keycloakで認証済み", icon=":material/verified_user:")
        if display_name:
            st.caption(str(display_name))
        if st.button("ログアウト", icon=":material/logout:", width="stretch"):
            st.logout()
else:
    with st.sidebar:
        st.divider()
        st.subheader("認証")
        st.warning("ローカル互換モード", icon=":material/warning:")
        st.caption("外部公開時はAUTH_MODE=oidcを使用してください。")

client = AgentApiClient(
    settings.web_api_base_url,
    access_token=access_token,
    timeout_seconds=settings.web_api_timeout_seconds,
)

pending = st.session_state.pop("pending_message", None)
if isinstance(pending, dict) and pending.get("conversation_id"):
    try:
        client.cancel_message(str(pending["conversation_id"]))
        st.toast("前回の回答生成を停止しました。", icon=":material/stop_circle:")
    except AgentApiError:
        st.toast("前回の回答生成は終了しています。", icon=":material/info:")

try:
    conversation_page = client.list_conversations()
except AgentApiError as error:
    show_api_error(error)
    if error.status_code == 401:
        st.info("ログアウト後、もう一度ログインしてください。")
    st.stop()

conversations = list(conversation_page.items)
query_conversation = st.query_params.get("conversation")
if query_conversation and all(str(item.id) != query_conversation for item in conversations):
    try:
        conversations.insert(0, client.get_conversation(query_conversation))
    except AgentApiError:
        query_conversation = None

if not conversations:
    try:
        conversations.append(client.create_conversation())
    except AgentApiError as error:
        show_api_error(error)
        st.stop()

conversation_by_id = {str(item.id): item for item in conversations}
conversation_ids = list(conversation_by_id)

next_conversation = st.session_state.pop("next_conversation_id", None)
if next_conversation in conversation_by_id:
    st.session_state.conversation_selector = next_conversation
elif st.session_state.get("conversation_selector") not in conversation_by_id:
    desired = query_conversation if query_conversation in conversation_by_id else None
    st.session_state.conversation_selector = desired or conversation_ids[0]

with st.sidebar:
    st.divider()
    st.header("会話")
    if st.button("新しい会話", icon=":material/add_comment:", width="stretch"):
        try:
            created = client.create_conversation()
            set_next_conversation(str(created.id))
            st.query_params["conversation"] = str(created.id)
            st.rerun()
        except AgentApiError as error:
            show_api_error(error)

    selected_conversation_id = st.selectbox(
        "会話を選択",
        conversation_ids,
        format_func=lambda item_id: conversation_by_id[item_id].title,
        key="conversation_selector",
    )
    selected_conversation = conversation_by_id[selected_conversation_id]
    if st.query_params.get("conversation") != selected_conversation_id:
        st.query_params["conversation"] = selected_conversation_id

    with st.expander("会話を管理"):
        with st.form(f"rename-{selected_conversation_id}", border=False):
            renamed_title = st.text_input(
                "会話名",
                value=selected_conversation.title,
                max_chars=200,
            )
            rename_submitted = st.form_submit_button(
                "名前を変更",
                icon=":material/edit:",
            )
        if rename_submitted:
            try:
                client.update_conversation(
                    selected_conversation_id,
                    title=renamed_title,
                )
                st.rerun()
            except AgentApiError as error:
                show_api_error(error)

        confirm_delete = st.checkbox(
            "この会話を削除することを確認",
            key=f"confirm-delete-{selected_conversation_id}",
        )
        if st.button(
            "会話を削除",
            icon=":material/delete:",
            disabled=not confirm_delete,
            width="stretch",
        ):
            try:
                client.delete_conversation(selected_conversation_id)
                set_next_conversation(None)
                st.query_params.pop("conversation", None)
                st.rerun()
            except AgentApiError as error:
                show_api_error(error)

        if selected_conversation.status == "archived":
            if st.button("会話を再開", icon=":material/unarchive:"):
                try:
                    client.update_conversation(
                        selected_conversation_id,
                        status="active",
                    )
                    st.rerun()
                except AgentApiError as error:
                    show_api_error(error)
        elif st.button("会話をアーカイブ", icon=":material/archive:"):
            try:
                client.update_conversation(
                    selected_conversation_id,
                    status="archived",
                )
                st.rerun()
            except AgentApiError as error:
                show_api_error(error)

    model_expander = st.expander(
        "利用可能なモデル",
        key="available-models",
        on_change="rerun",
    )
    if model_expander.open:
        with model_expander:
            try:
                model_list = client.list_models()
                if model_list.models:
                    for model_name in model_list.models:
                        st.code(model_name, language=None)
                else:
                    st.caption("利用可能なモデルはありません。")
            except AgentApiError as error:
                st.caption(error.user_message)

    notes_expander = st.expander(
        "保存されたメモ",
        key=f"saved-notes-{selected_conversation_id}",
        on_change="rerun",
    )
    if notes_expander.open:
        with notes_expander:
            try:
                notes = client.list_notes(selected_conversation_id)
                if not notes.items:
                    st.caption("保存されているメモはありません。")
                for note in notes.items:
                    st.markdown(f"**{note.title}**")
                    st.write(note.content)
            except AgentApiError as error:
                st.caption(error.user_message)

st.subheader(selected_conversation.title)
st.caption(
    f"会話ID: `{selected_conversation_id}` ・ "
    f"更新: {selected_conversation.updated_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
)

try:
    history = client.list_messages(selected_conversation_id)
except AgentApiError as error:
    show_api_error(error)
    st.stop()

historical_tool_names: list[str] = []
for message in history.items:
    if message.role == "user":
        with st.chat_message("user"):
            st.markdown(message.content)
    elif message.role == "assistant" and message.content and not message.tool_calls:
        with st.chat_message("assistant"):
            st.markdown(message.content)
    elif message.role == "tool" and message.name:
        historical_tool_names.append(message.name)

if historical_tool_names:
    with st.expander(f"過去のツール実行（{len(historical_tool_names)}件）"):
        for tool_name in historical_tool_names:
            st.write(f":material/build: `{tool_name}` 実行済み")
        st.caption("機密情報保護のため、ツール引数と実行出力は表示しません。")

is_archived = selected_conversation.status == "archived"
if is_archived:
    st.info("この会話はアーカイブされています。再開するとメッセージを送信できます。")

prompt = st.chat_input(
    "質問や指示を入力してください",
    key="chat_prompt",
    max_chars=settings.api_max_message_chars,
    disabled=is_archived,
    submit_mode="stop",
)

if prompt:
    idempotency_key = str(uuid.uuid4())
    st.session_state.pending_message = {
        "conversation_id": selected_conversation_id,
        "idempotency_key": idempotency_key,
    }
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response_slot = st.empty()
        response_parts: list[str] = []
        final_response = ""
        with st.status("回答を生成しています", expanded=True) as execution_status:
            try:
                for event in client.stream_message(
                    selected_conversation_id,
                    prompt,
                    idempotency_key=idempotency_key,
                ):
                    if event.event == "assistant.delta":
                        delta = event.data.get("delta")
                        if isinstance(delta, str):
                            response_parts.append(delta)
                            response_slot.markdown("".join(response_parts))
                    elif event.event == "tool.started":
                        tool_name = str(event.data.get("name") or "tool")
                        st.write(f":material/build: `{tool_name}` を実行中")
                    elif event.event == "tool.completed":
                        tool_name = str(event.data.get("name") or "tool")
                        st.write(f":material/check: `{tool_name}` が完了")
                    elif event.event == "message.completed":
                        content = event.data.get("content")
                        if isinstance(content, str):
                            final_response = content
                    elif event.event == "message.failed":
                        raise AgentApiError(
                            str(
                                event.data.get("message")
                                or "回答を生成できませんでした。"
                            ),
                            code=str(event.data.get("code") or "message_failed"),
                        )
                completed_content = final_response or "".join(response_parts)
                if completed_content:
                    response_slot.markdown(completed_content)
                else:
                    raise AgentApiError(
                        "APIから回答を受信できませんでした。",
                        code="empty_response",
                    )
                execution_status.update(
                    label="回答が完了しました",
                    state="complete",
                    expanded=False,
                )
                st.session_state.pop("pending_message", None)
                st.rerun()
            except AgentApiError as error:
                st.session_state.pop("pending_message", None)
                execution_status.update(
                    label="回答を生成できませんでした",
                    state="error",
                    expanded=True,
                )
                show_api_error(error)
