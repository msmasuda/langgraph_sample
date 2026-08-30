"""Synchronous API client used by the Streamlit web application."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from src.api.schemas import (
    CancelResponse,
    ConversationListResponse,
    ConversationResponse,
    ErrorResponse,
    HealthResponse,
    MessageHistoryResponse,
    ModelListResponse,
    NoteListResponse,
    ReadyResponse,
)

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class AgentApiError(Exception):
    """Safe error returned to the Streamlit UI."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "api_error",
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.user_message = message
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """One decoded Server-Sent Event."""

    event: str
    data: dict[str, Any]
    event_id: str | None = None


def parse_sse_lines(lines: Iterable[str]) -> Iterator[ServerSentEvent]:
    """Decode an SSE stream without exposing malformed server content."""
    event_name = "message"
    event_id: str | None = None
    data_lines: list[str] = []

    def decode_event() -> ServerSentEvent | None:
        nonlocal event_name, event_id, data_lines
        if not data_lines:
            event_name = "message"
            event_id = None
            return None
        try:
            data = json.loads("\n".join(data_lines))
        except (TypeError, ValueError) as error:
            raise AgentApiError(
                "APIから不正なストリーム応答を受信しました。",
                code="invalid_sse_response",
            ) from error
        if not isinstance(data, dict):
            raise AgentApiError(
                "APIから不正なストリーム応答を受信しました。",
                code="invalid_sse_response",
            )
        decoded = ServerSentEvent(event_name, data, event_id)
        event_name = "message"
        event_id = None
        data_lines = []
        return decoded

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            decoded = decode_event()
            if decoded is not None:
                yield decoded
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value or "message"
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)

    decoded = decode_event()
    if decoded is not None:
        yield decoded


class AgentApiClient:
    """Call the shared FastAPI contract from a server-side web client."""

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        timeout_seconds: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    def _headers(self, **additional: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Request-ID": str(uuid.uuid4()),
            **additional,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=False,
        )

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        request_id = response.headers.get("X-Request-ID")
        retry_after_value = response.headers.get("Retry-After")
        try:
            retry_after = int(retry_after_value) if retry_after_value else None
        except ValueError:
            retry_after = None
        try:
            detail = ErrorResponse.model_validate(response.json()).error
        except Exception:
            detail = None
        if detail is not None:
            raise AgentApiError(
                detail.message,
                code=detail.code,
                status_code=response.status_code,
                request_id=detail.request_id or request_id,
                retry_after=retry_after,
            )
        raise AgentApiError(
            "APIリクエストに失敗しました。時間をおいて再試行してください。",
            code="api_request_failed",
            status_code=response.status_code,
            request_id=request_id,
            retry_after=retry_after,
        )

    @staticmethod
    def _model(
        response: httpx.Response,
        model: type[ResponseModel],
    ) -> ResponseModel:
        try:
            return model.model_validate(response.json())
        except Exception as error:
            raise AgentApiError(
                "APIから不正な応答を受信しました。",
                code="invalid_api_response",
                status_code=response.status_code,
                request_id=response.headers.get("X-Request-ID"),
            ) from error

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        accepted_status_codes = kwargs.pop("accepted_status_codes", None)
        try:
            with self._client() as client:
                response = client.request(
                    method,
                    path,
                    headers=self._headers(**kwargs.pop("headers", {})),
                    **kwargs,
                )
        except httpx.RequestError as error:
            raise AgentApiError(
                "APIに接続できません。APIサーバーの状態を確認してください。",
                code="api_unavailable",
            ) from error
        if (
            accepted_status_codes is None
            or response.status_code not in accepted_status_codes
        ):
            self._raise_for_response(response)
        return response

    def health(self) -> HealthResponse:
        return self._model(self._request("GET", "/health"), HealthResponse)

    def ready(self) -> ReadyResponse:
        response = self._request(
            "GET",
            "/ready",
            accepted_status_codes={200, 503},
        )
        return self._model(response, ReadyResponse)

    def list_models(self) -> ModelListResponse:
        response = self._request("GET", "/v1/models")
        return self._model(response, ModelListResponse)

    def create_conversation(self, title: str = "新しい会話") -> ConversationResponse:
        response = self._request(
            "POST",
            "/v1/conversations",
            json={"title": title},
        )
        return self._model(response, ConversationResponse)

    def list_conversations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ConversationListResponse:
        response = self._request(
            "GET",
            "/v1/conversations",
            params={"limit": limit, "offset": offset},
        )
        return self._model(response, ConversationListResponse)

    def get_conversation(self, conversation_id: str) -> ConversationResponse:
        response = self._request("GET", f"/v1/conversations/{conversation_id}")
        return self._model(response, ConversationResponse)

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
    ) -> ConversationResponse:
        payload = {
            key: value
            for key, value in {"title": title, "status": status}.items()
            if value is not None
        }
        response = self._request(
            "PATCH",
            f"/v1/conversations/{conversation_id}",
            json=payload,
        )
        return self._model(response, ConversationResponse)

    def delete_conversation(self, conversation_id: str) -> None:
        self._request("DELETE", f"/v1/conversations/{conversation_id}")

    def list_messages(self, conversation_id: str) -> MessageHistoryResponse:
        response = self._request(
            "GET",
            f"/v1/conversations/{conversation_id}/messages",
        )
        return self._model(response, MessageHistoryResponse)

    def cancel_message(self, conversation_id: str) -> CancelResponse:
        response = self._request(
            "POST",
            f"/v1/conversations/{conversation_id}/cancel",
        )
        return self._model(response, CancelResponse)

    def list_notes(self, conversation_id: str) -> NoteListResponse:
        response = self._request(
            "GET",
            f"/v1/conversations/{conversation_id}/notes",
        )
        return self._model(response, NoteListResponse)

    def stream_message(
        self,
        conversation_id: str,
        content: str,
        *,
        idempotency_key: str,
    ) -> Iterator[ServerSentEvent]:
        headers = self._headers(
            Accept="text/event-stream",
            **{"Idempotency-Key": idempotency_key},
        )
        try:
            with self._client() as client:
                with client.stream(
                    "POST",
                    f"/v1/conversations/{conversation_id}/messages/stream",
                    headers=headers,
                    json={"content": content},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        response.read()
                        self._raise_for_response(response)
                    yield from parse_sse_lines(response.iter_lines())
        except AgentApiError:
            raise
        except httpx.RequestError as error:
            raise AgentApiError(
                "APIとのストリーム接続が切断されました。",
                code="api_stream_disconnected",
            ) from error
