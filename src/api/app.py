"""FastAPI application for Web and mobile agent clients."""

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response, Security, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.api.auth import (
    AuthenticationError,
    AuthenticationUnavailable,
    OpenIDConnectAuthenticator,
)
from src.api.runtime import build_lifespan
from src.api.schemas import (
    CancelResponse,
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationResponse,
    ConversationUpdateRequest,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    MessageHistoryItem,
    MessageHistoryResponse,
    MessageRequest,
    MessageResponse,
    ModelListResponse,
    NoteCreateRequest,
    NoteListResponse,
    NoteResponse,
    NoteUpdateRequest,
    ReadyResponse,
    ToolExecutionResponse,
)
from src.api.sse import encode_sse
from src.config import Settings, get_settings
from src.errors import (
    AgentConnectionError,
    AgentExecutionError,
    AgentLimitError,
    AgentServiceError,
    AgentTimeoutError,
)
from src.services import (
    AgentEvent,
    AgentRunResult,
    AgentService,
    ConversationExecutionRegistry,
    IdempotencyStore,
    InMemoryConversationStore,
    InMemoryNoteStore,
    OllamaModelService,
)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


class ApiProblem(RuntimeError):
    """Expected API problem rendered through the common error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        super().__init__(message)


class RunCancelled(RuntimeError):
    """Internal signal for explicit conversation cancellation."""


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", uuid.uuid4()))


def _message_fingerprint(message: MessageRequest) -> str:
    return hashlib.sha256(message.content.encode("utf-8")).hexdigest()


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def _agent_error_status(error: AgentServiceError) -> int:
    if isinstance(error, AgentTimeoutError):
        return status.HTTP_504_GATEWAY_TIMEOUT
    if isinstance(error, AgentConnectionError):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if isinstance(error, AgentLimitError):
        return status.HTTP_429_TOO_MANY_REQUESTS
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _tool_responses(result: AgentRunResult) -> list[ToolExecutionResponse]:
    tool_events: list[ToolExecutionResponse] = []
    by_call_id: dict[str, ToolExecutionResponse] = {}
    for event in result.events:
        if event.type == "tool_started":
            tool_response = ToolExecutionResponse(
                name=event.tool_name or "unknown",
                args=dict(event.tool_args),
            )
            tool_events.append(tool_response)
            if event.tool_call_id:
                by_call_id[event.tool_call_id] = tool_response
        elif event.type == "tool_completed":
            tool_response = by_call_id.get(event.tool_call_id or "")
            if tool_response is None:
                tool_response = ToolExecutionResponse(name=event.tool_name or "tool")
                tool_events.append(tool_response)
            tool_response.output = event.content
    return tool_events


def _conversation_response(record: Any) -> ConversationResponse:
    return ConversationResponse(
        id=record.id,
        title=record.title,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
    )


def _note_response(record: Any) -> NoteResponse:
    return NoteResponse(
        id=record.id,
        conversation_id=record.conversation_id,
        title=record.title,
        content=record.content,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def _history_items(snapshot: Any) -> list[MessageHistoryItem]:
    values = getattr(snapshot, "values", {}) or {}
    messages = values.get("messages", []) if isinstance(values, dict) else []
    items: list[MessageHistoryItem] = []
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, ToolMessage):
            role = "tool"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            continue
        items.append(
            MessageHistoryItem(
                id=str(getattr(message, "id", None) or index),
                role=role,
                content=_message_text(getattr(message, "content", "")),
                name=getattr(message, "name", None),
                tool_call_id=getattr(message, "tool_call_id", None),
                tool_calls=list(getattr(message, "tool_calls", []) or []),
            )
        )
    return items


def create_app(
    *,
    settings: Settings | None = None,
    agent_service: Any | None = None,
    model_service: Any | None = None,
    conversation_store: Any | None = None,
    note_store: Any | None = None,
    execution_registry: Any | None = None,
    idempotency_store: Any | None = None,
    run_store: Any | None = None,
    authenticator: Any | None = None,
    use_database: bool | None = None,
) -> FastAPI:
    """Create an API application with replaceable services for tests."""
    active_settings = settings or get_settings()
    managed_database = (
        bool(active_settings.database_url)
        and agent_service is None
        and conversation_store is None
        if use_database is None
        else use_database
    )
    app = FastAPI(
        title="LangGraph Ollama Agent API",
        version="0.4.0",
        description="Web・モバイル向けLangGraphエージェントAPI",
        lifespan=build_lifespan(active_settings, enabled=managed_database),
    )
    app.state.settings = active_settings
    app.state.agent_service = agent_service or (
        None if managed_database else AgentService.create(settings=active_settings)
    )
    app.state.model_service = model_service or OllamaModelService(
        active_settings.ollama_base_url,
        active_settings.ollama_health_timeout_seconds,
    )
    app.state.conversation_store = conversation_store or InMemoryConversationStore()
    app.state.note_store = note_store or InMemoryNoteStore()
    app.state.execution_registry = execution_registry or ConversationExecutionRegistry()
    app.state.idempotency_store = idempotency_store or IdempotencyStore(
        ttl_seconds=active_settings.idempotency_ttl_seconds,
        max_entries=active_settings.idempotency_max_entries,
    )
    app.state.run_store = run_store
    app.state.database_manager = None
    app.state.checkpoint_manager = None
    app.state.authenticator = authenticator or OpenIDConnectAuthenticator(
        active_settings
    )
    bearer_scheme = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else str(uuid.uuid4())
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiProblem)
    async def handle_api_problem(request: Request, error: ApiProblem) -> JSONResponse:
        return _error_response(
            request,
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            headers=error.headers,
        )

    @app.exception_handler(AgentServiceError)
    async def handle_agent_error(
        request: Request,
        error: AgentServiceError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=_agent_error_status(error),
            code=error.code,
            message=error.user_message,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        _error: Exception,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="内部処理でエラーが発生しました。",
        )

    async def resolve_current_user(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(bearer_scheme),
        ] = None,
    ) -> uuid.UUID:
        if active_settings.auth_mode == "disabled":
            await app.state.conversation_store.ensure_user(
                active_settings.default_user_id,
                active_settings.default_user_subject,
            )
            return active_settings.default_user_id
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ApiProblem(
                status.HTTP_401_UNAUTHORIZED,
                "authentication_required",
                "Bearerアクセストークンが必要です。",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            principal = await app.state.authenticator.authenticate(
                credentials.credentials
            )
        except AuthenticationUnavailable as error:
            raise ApiProblem(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "authentication_unavailable",
                "認証サーバーを利用できません。",
            ) from error
        except AuthenticationError as error:
            raise ApiProblem(
                status.HTTP_401_UNAUTHORIZED,
                "invalid_access_token",
                "アクセストークンが無効です。",
                headers={"WWW-Authenticate": "Bearer error=\"invalid_token\""},
            ) from error
        return await app.state.conversation_store.get_or_create_user(
            principal.subject,
            display_name=principal.display_name,
        )

    CurrentUserId = Annotated[uuid.UUID, Depends(resolve_current_user)]

    async def require_conversation(
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Any:
        record = await app.state.conversation_store.get(
            conversation_id,
            user_id=user_id,
        )
        if record is None:
            raise ApiProblem(
                status.HTTP_404_NOT_FOUND,
                "conversation_not_found",
                "指定された会話が見つかりません。",
            )
        return record

    async def reserve_conversation(conversation_id: uuid.UUID) -> None:
        if not await app.state.execution_registry.reserve(conversation_id):
            raise ApiProblem(
                status.HTTP_409_CONFLICT,
                "conversation_busy",
                "この会話では別のメッセージを処理中です。",
            )

    def validate_active_conversation(record: Any) -> None:
        if record.status != "active":
            raise ApiProblem(
                status.HTTP_409_CONFLICT,
                "conversation_archived",
                "アーカイブ済みの会話にはメッセージを送信できません。",
            )

    def validate_message_length(message: MessageRequest) -> None:
        if len(message.content) > active_settings.api_max_message_chars:
            raise ApiProblem(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "message_too_long",
                "メッセージが長すぎます。",
            )

    async def invoke_with_cancellation(
        conversation_id: uuid.UUID,
        prompt: str,
        thread_id: str,
        user_id: uuid.UUID,
    ) -> AgentRunResult:
        task = asyncio.create_task(
            app.state.agent_service.ainvoke(
                prompt,
                thread_id,
                user_id=str(user_id),
            )
        )
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.25)
                if await app.state.execution_registry.is_cancel_requested(
                    conversation_id
                ):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise ApiProblem(
                        status.HTTP_409_CONFLICT,
                        "message_cancelled",
                        "メッセージ処理をキャンセルしました。",
                    )
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def cancellable_events(
        conversation_id: uuid.UUID,
        prompt: str,
        thread_id: str,
        user_id: uuid.UUID,
        request: Request,
    ) -> AsyncIterator[AgentEvent]:
        iterator = app.state.agent_service.astream_events(
            prompt,
            thread_id,
            include_tokens=True,
            user_id=str(user_id),
        ).__aiter__()
        try:
            while True:
                next_event = asyncio.create_task(anext(iterator))
                while not next_event.done():
                    await asyncio.wait({next_event}, timeout=0.25)
                    if await request.is_disconnected():
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
                        return
                    if await app.state.execution_registry.is_cancel_requested(
                        conversation_id
                    ):
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
                        raise RunCancelled()
                try:
                    yield await next_event
                except StopAsyncIteration:
                    return
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/ready",
        response_model=ReadyResponse,
        response_model_exclude_none=True,
        responses={503: {"model": ReadyResponse}},
        tags=["system"],
    )
    async def ready(response: Response) -> ReadyResponse:
        ollama_status = await app.state.model_service.aget_status()
        database_available: bool | None = None
        if active_settings.database_url:
            database_available = bool(
                app.state.database_manager and await app.state.database_manager.ping()
            )
        if not ollama_status.available or database_available is False:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return ReadyResponse(
                status="not_ready",
                ollama=ollama_status.available,
                database=database_available,
            )
        return ReadyResponse(status="ready", ollama=True, database=database_available)

    @app.get(
        "/v1/models",
        response_model=ModelListResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["models"],
    )
    async def list_models(_user_id: CurrentUserId) -> ModelListResponse:
        ollama_status = await app.state.model_service.aget_status()
        if not ollama_status.available:
            raise ApiProblem(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ollama_unavailable",
                "Ollamaに接続できません。",
            )
        return ModelListResponse(models=list(ollama_status.models))

    @app.post(
        "/v1/conversations",
        response_model=ConversationResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["conversations"],
    )
    async def create_conversation(
        user_id: CurrentUserId,
        payload: ConversationCreateRequest | None = None,
    ) -> ConversationResponse:
        record = await app.state.conversation_store.create(
            user_id=user_id,
            title=payload.title if payload else "新しい会話",
            retention_days=active_settings.conversation_retention_days,
        )
        return _conversation_response(record)

    @app.get(
        "/v1/conversations",
        response_model=ConversationListResponse,
        tags=["conversations"],
    )
    async def list_conversations(
        user_id: CurrentUserId,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ConversationListResponse:
        records, total = await app.state.conversation_store.list(
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
        return ConversationListResponse(
            items=[_conversation_response(record) for record in records],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/v1/conversations/{conversation_id}",
        response_model=ConversationResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["conversations"],
    )
    async def get_conversation(
        conversation_id: uuid.UUID,
        user_id: CurrentUserId,
    ) -> ConversationResponse:
        return _conversation_response(
            await require_conversation(conversation_id, user_id)
        )

    @app.patch(
        "/v1/conversations/{conversation_id}",
        response_model=ConversationResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["conversations"],
    )
    async def update_conversation(
        conversation_id: uuid.UUID,
        payload: ConversationUpdateRequest,
        user_id: CurrentUserId,
    ) -> ConversationResponse:
        if payload.title is None and payload.status is None:
            raise ApiProblem(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "empty_update",
                "更新する項目を指定してください。",
            )
        record = await app.state.conversation_store.update(
            conversation_id,
            user_id=user_id,
            title=payload.title,
            status=payload.status,
        )
        if record is None:
            await require_conversation(conversation_id, user_id)
            raise AssertionError("unreachable")
        return _conversation_response(record)

    @app.delete(
        "/v1/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["conversations"],
    )
    async def delete_conversation(
        conversation_id: uuid.UUID,
        user_id: CurrentUserId,
    ) -> Response:
        record = await require_conversation(conversation_id, user_id)
        await reserve_conversation(conversation_id)
        try:
            await app.state.agent_service.adelete_thread(record.thread_id)
            await app.state.note_store.delete_for_conversation(conversation_id)
            await app.state.idempotency_store.delete_resource(str(conversation_id))
            deleted = await app.state.conversation_store.delete(
                conversation_id,
                user_id=user_id,
            )
            if not deleted:
                raise ApiProblem(
                    status.HTTP_404_NOT_FOUND,
                    "conversation_not_found",
                    "指定された会話が見つかりません。",
                )
        finally:
            await app.state.execution_registry.release(conversation_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/v1/conversations/{conversation_id}/messages",
        response_model=MessageHistoryResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["messages"],
    )
    async def list_messages(
        conversation_id: uuid.UUID,
        user_id: CurrentUserId,
    ) -> MessageHistoryResponse:
        record = await require_conversation(conversation_id, user_id)
        snapshot = await app.state.agent_service.aget_state(
            record.thread_id,
            user_id=str(user_id),
        )
        return MessageHistoryResponse(
            conversation_id=conversation_id,
            items=_history_items(snapshot),
        )

    @app.post(
        "/v1/conversations/{conversation_id}/cancel",
        response_model=CancelResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["messages"],
    )
    async def cancel_message(
        conversation_id: uuid.UUID,
        user_id: CurrentUserId,
    ) -> CancelResponse:
        await require_conversation(conversation_id, user_id)
        requested = await app.state.execution_registry.request_cancel(conversation_id)
        return CancelResponse(
            conversation_id=conversation_id,
            status="cancellation_requested" if requested else "not_running",
        )

    @app.post(
        "/v1/conversations/{conversation_id}/messages",
        response_model=MessageResponse,
        responses={
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
        tags=["messages"],
    )
    async def create_message(
        conversation_id: uuid.UUID,
        message: MessageRequest,
        response: Response,
        user_id: CurrentUserId,
        idempotency_key: IdempotencyKey = None,
    ) -> MessageResponse:
        record = await require_conversation(conversation_id, user_id)
        validate_active_conversation(record)
        validate_message_length(message)
        fingerprint = _message_fingerprint(message)
        if idempotency_key:
            cached = await app.state.idempotency_store.get(
                "message", str(conversation_id), idempotency_key
            )
            if cached is not None:
                cached_fingerprint, cached_response = cached
                if cached_fingerprint != fingerprint:
                    raise ApiProblem(
                        status.HTTP_409_CONFLICT,
                        "idempotency_conflict",
                        (
                            "同じ冪等性キーが異なるメッセージに"
                            "使用されています。"
                        ),
                    )
                try:
                    completed = (
                        cached_response
                        if isinstance(cached_response, MessageResponse)
                        else MessageResponse.model_validate(cached_response)
                    )
                except Exception as error:
                    raise ApiProblem(
                        status.HTTP_409_CONFLICT,
                        "idempotency_conflict",
                        "冪等性キーを再利用できません。",
                    ) from error
                response.headers["Idempotency-Replayed"] = "true"
                return completed

        await reserve_conversation(conversation_id)
        started_at = time.perf_counter()
        try:
            result = await invoke_with_cancellation(
                conversation_id,
                message.content,
                record.thread_id,
                user_id,
            )
            tool_events = _tool_responses(result)
            completed = MessageResponse(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                content=result.response,
                tool_events=tool_events,
            )
            if app.state.run_store is not None:
                await app.state.run_store.record_completed(
                    conversation_id=conversation_id,
                    message_id=completed.id,
                    model=active_settings.ollama_model,
                    prompt=message.content,
                    response=result.response,
                    duration_ms=int((time.perf_counter() - started_at) * 1_000),
                    tool_events=tool_events,
                )
            await app.state.conversation_store.touch(
                conversation_id, user_id=user_id
            )
            if idempotency_key:
                await app.state.idempotency_store.put(
                    "message",
                    str(conversation_id),
                    idempotency_key,
                    fingerprint,
                    completed,
                )
            return completed
        finally:
            await app.state.execution_registry.release(conversation_id)

    @app.post(
        "/v1/conversations/{conversation_id}/messages/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-Sent Events stream",
                "content": {"text/event-stream": {}},
            },
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
        },
        tags=["messages"],
    )
    async def stream_message(
        conversation_id: uuid.UUID,
        message: MessageRequest,
        request: Request,
        user_id: CurrentUserId,
        idempotency_key: IdempotencyKey = None,
    ) -> StreamingResponse:
        record = await require_conversation(conversation_id, user_id)
        validate_active_conversation(record)
        validate_message_length(message)
        request_id = _request_id(request)
        fingerprint = _message_fingerprint(message)
        if idempotency_key:
            cached = await app.state.idempotency_store.get(
                "stream", str(conversation_id), idempotency_key
            )
            if cached is not None:
                cached_fingerprint, cached_chunks = cached
                if cached_fingerprint != fingerprint:
                    raise ApiProblem(
                        status.HTTP_409_CONFLICT,
                        "idempotency_conflict",
                        (
                            "同じ冪等性キーが異なるメッセージに"
                            "使用されています。"
                        ),
                    )
                if not isinstance(cached_chunks, (tuple, list)) or not all(
                    isinstance(chunk, str) for chunk in cached_chunks
                ):
                    raise ApiProblem(
                        status.HTTP_409_CONFLICT,
                        "idempotency_conflict",
                        "冪等性キーを再利用できません。",
                    )

                async def replay() -> AsyncIterator[str]:
                    for chunk in cached_chunks:
                        yield chunk
                        await asyncio.sleep(0)

                return StreamingResponse(
                    replay(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Idempotency-Replayed": "true",
                        "X-Accel-Buffering": "no",
                    },
                )

        await reserve_conversation(conversation_id)
        message_id = uuid.uuid4()

        async def event_stream() -> AsyncIterator[str]:
            chunks: list[str] = []
            final_content = ""
            delta_content: list[str] = []
            observed_events: list[AgentEvent] = []
            sequence = 0
            started_at = time.perf_counter()

            def event_chunk(event: str, data: dict[str, Any]) -> str:
                nonlocal sequence
                sequence += 1
                return encode_sse(event, data, event_id=f"{message_id}:{sequence}")

            try:
                if await request.is_disconnected():
                    return
                started = event_chunk(
                    "message.started",
                    {
                        "message_id": str(message_id),
                        "conversation_id": str(conversation_id),
                        "request_id": request_id,
                    },
                )
                chunks.append(started)
                yield started
                async for event in cancellable_events(
                    conversation_id,
                    message.content,
                    record.thread_id,
                    user_id,
                    request,
                ):
                    observed_events.append(event)
                    if event.type == "assistant_delta":
                        delta_content.append(event.content)
                        chunk = event_chunk(
                            "assistant.delta",
                            {"message_id": str(message_id), "delta": event.content},
                        )
                    elif event.type == "tool_started":
                        chunk = event_chunk(
                            "tool.started",
                            {
                                "message_id": str(message_id),
                                "tool_call_id": event.tool_call_id,
                                "name": event.tool_name,
                                "args": dict(event.tool_args),
                            },
                        )
                    elif event.type == "tool_completed":
                        chunk = event_chunk(
                            "tool.completed",
                            {
                                "message_id": str(message_id),
                                "tool_call_id": event.tool_call_id,
                                "name": event.tool_name,
                                "output": event.content,
                            },
                        )
                    elif event.type == "assistant_completed":
                        final_content = event.content
                        continue
                    else:
                        continue
                    chunks.append(chunk)
                    yield chunk

                if await request.is_disconnected():
                    return
                final_content = final_content or "".join(delta_content)
                tool_events = _tool_responses(
                    AgentRunResult(
                        response=final_content,
                        events=tuple(observed_events),
                    )
                )
                if app.state.run_store is not None:
                    await app.state.run_store.record_completed(
                        conversation_id=conversation_id,
                        message_id=message_id,
                        model=active_settings.ollama_model,
                        prompt=message.content,
                        response=final_content,
                        duration_ms=int((time.perf_counter() - started_at) * 1_000),
                        tool_events=tool_events,
                    )
                await app.state.conversation_store.touch(
                    conversation_id, user_id=user_id
                )
                completed = event_chunk(
                    "message.completed",
                    {
                        "message_id": str(message_id),
                        "conversation_id": str(conversation_id),
                        "content": final_content,
                    },
                )
                chunks.append(completed)
                if idempotency_key:
                    await app.state.idempotency_store.put(
                        "stream",
                        str(conversation_id),
                        idempotency_key,
                        fingerprint,
                        tuple(chunks),
                    )
                yield completed
            except RunCancelled:
                yield event_chunk(
                    "message.failed",
                    {
                        "message_id": str(message_id),
                        "code": "message_cancelled",
                        "message": "メッセージ処理をキャンセルしました。",
                    },
                )
            except asyncio.CancelledError:
                raise
            except AgentServiceError as error:
                yield event_chunk(
                    "message.failed",
                    {
                        "message_id": str(message_id),
                        "code": error.code,
                        "message": error.user_message,
                    },
                )
            except Exception:
                safe_error = AgentExecutionError()
                yield event_chunk(
                    "message.failed",
                    {
                        "message_id": str(message_id),
                        "code": safe_error.code,
                        "message": safe_error.user_message,
                    },
                )
            finally:
                await app.state.execution_registry.release(conversation_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/v1/conversations/{conversation_id}/notes",
        response_model=NoteListResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["notes"],
    )
    async def list_notes(
        conversation_id: uuid.UUID,
        user_id: CurrentUserId,
    ) -> NoteListResponse:
        await require_conversation(conversation_id, user_id)
        notes = await app.state.note_store.list(
            conversation_id, user_id=user_id
        )
        return NoteListResponse(items=[_note_response(note) for note in notes])

    @app.post(
        "/v1/conversations/{conversation_id}/notes",
        response_model=NoteResponse,
        status_code=status.HTTP_201_CREATED,
        responses={404: {"model": ErrorResponse}},
        tags=["notes"],
    )
    async def create_note(
        conversation_id: uuid.UUID,
        payload: NoteCreateRequest,
        user_id: CurrentUserId,
    ) -> NoteResponse:
        await require_conversation(conversation_id, user_id)
        note = await app.state.note_store.create(
            conversation_id,
            user_id=user_id,
            title=payload.title,
            content=payload.content,
        )
        return _note_response(note)

    @app.patch(
        "/v1/conversations/{conversation_id}/notes/{note_id}",
        response_model=NoteResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["notes"],
    )
    async def update_note(
        conversation_id: uuid.UUID,
        note_id: uuid.UUID,
        payload: NoteUpdateRequest,
        user_id: CurrentUserId,
    ) -> NoteResponse:
        await require_conversation(conversation_id, user_id)
        if payload.title is None and payload.content is None:
            raise ApiProblem(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "empty_update",
                "更新する項目を指定してください。",
            )
        note = await app.state.note_store.update(
            conversation_id,
            note_id,
            user_id=user_id,
            title=payload.title,
            content=payload.content,
        )
        if note is None:
            raise ApiProblem(
                status.HTTP_404_NOT_FOUND,
                "note_not_found",
                "指定されたメモが見つかりません。",
            )
        return _note_response(note)

    @app.delete(
        "/v1/conversations/{conversation_id}/notes/{note_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={404: {"model": ErrorResponse}},
        tags=["notes"],
    )
    async def delete_note(
        conversation_id: uuid.UUID,
        note_id: uuid.UUID,
        user_id: CurrentUserId,
    ) -> Response:
        await require_conversation(conversation_id, user_id)
        deleted = await app.state.note_store.delete(
            conversation_id,
            note_id,
            user_id=user_id,
        )
        if not deleted:
            raise ApiProblem(
                status.HTTP_404_NOT_FOUND,
                "note_not_found",
                "指定されたメモが見つかりません。",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_app()
