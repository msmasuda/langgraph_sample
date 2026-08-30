"""Rate limiting, client address handling, and privacy-safe JSON logging."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from src.services.rate_limit import RateLimitResult, hash_identifier

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "email",
    "phone",
    "address",
    "display_name",
    "tool_args",
    "arguments",
    "prompt",
    "content",
}
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def client_ip(request: Request, trusted_proxies: tuple[str, ...]) -> str:
    """Return a validated peer address, trusting XFF only from configured proxies."""
    peer = request.client.host if request.client else "unknown"
    if peer not in trusted_proxies:
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer


def sanitize_for_log(value: Any, *, key: str | None = None) -> Any:
    """Recursively remove secrets and free-form user/tool content from log context."""
    if key:
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized_key in _SENSITIVE_KEYS or any(
            fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS
        ):
            return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_for_log(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def redact_log_message(message: str) -> str:
    """Mask common credentials and email addresses in fixed event messages."""
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", message)
    return _EMAIL_PATTERN.sub("[REDACTED]", redacted)


class JsonLogFormatter(logging.Formatter):
    """Format application events as one privacy-safe JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_log_message(record.getMessage()),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(sanitize_for_log(context))
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_api_logger(level: str, *, enabled: bool) -> logging.Logger:
    """Configure an isolated API logger without changing host application logs."""
    logger = logging.getLogger("langgraph.api")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, level))
    if enabled:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        logger.addHandler(handler)
    else:
        logger.addHandler(logging.NullHandler())
    return logger


def rate_limit_headers(
    result: RateLimitResult,
    *,
    rejected: bool = False,
) -> dict[str, str]:
    """Return client-visible limit metadata for the strictest applied limit."""
    headers = {
        "RateLimit": (
            f'"default";r={result.remaining};t={result.retry_after}'
        ),
        "RateLimit-Policy": (
            f'"default";q={result.limit};w={result.window_seconds}'
        ),
        "RateLimit-Limit": str(result.limit),
        "RateLimit-Remaining": str(result.remaining),
        "RateLimit-Reset": str(result.retry_after),
    }
    if rejected:
        headers["Retry-After"] = str(result.retry_after)
    return headers
