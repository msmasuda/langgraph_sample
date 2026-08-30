"""FastAPI application for Web and mobile agent clients."""

import asyncio
import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.schemas import (
    ConversationResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    MessageRequest,
    MessageResponse,
    ModelListResponse,
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

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


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
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


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
                tool_response = ToolExecutionResponse(
                    name=event.tool_name or "tool",
                )
                tool_events.append(tool_response)
            tool_response.output = event.content
    return tool_events


def create_app(
    *,
    settings: Settings | None = None,
    agent_service: Any | None = None,
    model_service: Any | None = None,
    conversation_store: InMemoryConversationStore | None = None,
    execution_registry: ConversationExecutionRegistry | None = None,
    idempotency_store: IdempotencyStore | None = None,
) -> FastAPI:
    """Create an API application with replaceable services for tests."""
    active_settings = settings or get_settings()
    app = FastAPI(
        title="LangGraph Ollama Agent API",
        version="0.2.0",
        description="Web・モバイル向けLangGraphエージェントAPI",
    )

    app.state.settings = active_settings
    app.state.agent_service = agent_service or AgentService.create(
        settings=active_settings
    )
    app.state.model_service = model_service or OllamaModelService(
        active_settings.ollama_base_url,
        active_settings.ollama_health_timeout_seconds,
    )
    app.state.conversation_store = conversation_store or InMemoryConversationStore()
    app.state.execution_registry = (
        execution_registry or ConversationExecutionRegistry()
    )
    app.state.idempotency_store = idempotency_store or IdempotencyStore(
        ttl_seconds=active_settings.idempotency_ttl_seconds,
        max_entries=active_settings.idempotency_max_entries,
    )

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

    async def require_conversation(conversation_id: uuid.UUID) -> None:
        record = await app.state.conversation_store.get(conversation_id)
        if record is None:
            raise ApiProblem(
                status.HTTP_404_NOT_FOUND,
                "conversation_not_found",
                "指定された会話が見つかりません。",
            )

    async def reserve_conversation(conversation_id: uuid.UUID) -> None:
        if not await app.state.execution_registry.reserve(conversation_id):
            raise ApiProblem(
                status.HTTP_409_CONFLICT,
                "conversation_busy",
                "この会話では別のメッセージを処理中です。",
            )

    def validate_message_length(message: MessageRequest) -> None:
        if len(message.content) > active_settings.api_max_message_chars:
            raise ApiProblem(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "message_too_long",
                "メッセージが長すぎます。",
            )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/ready",
        response_model=ReadyResponse,
        responses={503: {"model": ReadyResponse}},
        tags=["system"],
    )
    async def ready(response: Response) -> ReadyResponse:
        ollama_status = await app.state.model_service.aget_status()
        if not ollama_status.available:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return ReadyResponse(status="not_ready", ollama=False)
        return ReadyResponse(status="ready", ollama=True)

    @app.get(
        "/v1/models",
        response_model=ModelListResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["models"],
    )
    async def list_models() -> ModelListResponse:
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
    async def create_conversation() -> ConversationResponse:
        record = await app.state.conversation_store.create()
        return ConversationResponse(id=record.id, created_at=record.created_at)

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
        idempotency_key: IdempotencyKey = None,
    ) -> MessageResponse:
        await require_conversation(conversation_id)
        validate_message_length(message)
        fingerprint = _message_fingerprint(message)

        if idempotency_key:
            cached = await app.state.idempotency_store.get(
                "message",
                str(conversation_id),
                idempotency_key,
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
                if not isinstance(cached_response, MessageResponse):
                    raise ApiProblem(
                        status.HTTP_409_CONFLICT,
                        "idempotency_conflict",
                        "冪等性キーを再利用できません。",
                    )
                response.headers["Idempotency-Replayed"] = "true"
                return cached_response

        await reserve_conversation(conversation_id)
        try:
            result = await app.state.agent_service.ainvoke(
                message.content,
                str(conversation_id),
            )
            completed = MessageResponse(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                content=result.response,
                tool_events=_tool_responses(result),
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
        idempotency_key: IdempotencyKey = None,
    ) -> StreamingResponse:
        await require_conversation(conversation_id)
        validate_message_length(message)
        request_id = _request_id(request)
        fingerprint = _message_fingerprint(message)

        if idempotency_key:
            cached = await app.state.idempotency_store.get(
                "stream",
                str(conversation_id),
                idempotency_key,
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
                if not isinstance(cached_chunks, tuple):
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
            sequence = 0

            def event_chunk(event: str, data: dict[str, Any]) -> str:
                nonlocal sequence
                sequence += 1
                return encode_sse(
                    event,
                    data,
                    event_id=f"{message_id}:{sequence}",
                )

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

                async for event in app.state.agent_service.astream_events(
                    message.content,
                    str(conversation_id),
                    include_tokens=True,
                ):
                    if await request.is_disconnected():
                        return

                    if event.type == "assistant_delta":
                        delta_content.append(event.content)
                        chunk = event_chunk(
                            "assistant.delta",
                            {
                                "message_id": str(message_id),
                                "delta": event.content,
                            },
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

                completed = event_chunk(
                    "message.completed",
                    {
                        "message_id": str(message_id),
                        "conversation_id": str(conversation_id),
                        "content": final_content or "".join(delta_content),
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
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
