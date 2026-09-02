"""CORS, rate-limit, logging, and tool-approval policy tests."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from langchain_core.tools import tool

from src.agent import create_agent
from src.api.protection import JsonLogFormatter, sanitize_for_log
from src.config import Settings
from src.services.rate_limit import InMemoryRateLimiter
from src.tool_policy import (
    ToolApprovalPolicy,
    UnapprovedToolRegistrationError,
)
from tests.test_api import make_test_app


@pytest.mark.asyncio
async def test_cors_allows_only_configured_browser_origin():
    app = make_test_app(
        settings_overrides={
            "cors_allowed_origins": "https://app.example.com",
            "rate_limit_enabled": True,
            "rate_limit_ip_requests": 1,
            "rate_limit_user_requests": 1,
        }
    )
    transport = httpx.ASGITransport(app=app)
    preflight_headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.options(
            "/v1/conversations",
            headers={"Origin": "https://app.example.com", **preflight_headers},
        )
        denied = await client.options(
            "/v1/conversations",
            headers={"Origin": "https://evil.example", **preflight_headers},
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "RateLimit-Limit" not in allowed.headers
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.asyncio
async def test_user_rate_limit_returns_safe_429_and_retry_headers():
    app = make_test_app(
        settings_overrides={
            "rate_limit_enabled": True,
            "rate_limit_ip_requests": 10,
            "rate_limit_user_requests": 2,
            "rate_limit_window_seconds": 60,
        }
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/models")
        second = await client.get("/v1/models")
        rejected = await client.get("/v1/models")

    assert first.status_code == 200
    assert second.status_code == 200
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "rate_limit_exceeded"
    assert rejected.headers["RateLimit-Policy"] == '"default";q=2;w=60'
    assert rejected.headers["RateLimit"].startswith('"default";r=0;t=')
    assert rejected.headers["RateLimit-Limit"] == "2"
    assert rejected.headers["RateLimit-Remaining"] == "0"
    assert int(rejected.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_ip_rate_limit_applies_before_authentication():
    app = make_test_app(
        settings_overrides={
            "rate_limit_enabled": True,
            "rate_limit_ip_requests": 1,
            "rate_limit_user_requests": 10,
        }
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/models")
        rejected = await client.get("/v1/models")

    assert first.status_code == 200
    assert rejected.status_code == 429
    assert rejected.headers["RateLimit-Limit"] == "1"


@pytest.mark.asyncio
async def test_in_memory_limiter_separates_subjects():
    limiter = InMemoryRateLimiter()

    first_alice = await limiter.hit("user", "alice", limit=1, window_seconds=60)
    second_alice = await limiter.hit("user", "alice", limit=1, window_seconds=60)
    first_bob = await limiter.hit("user", "bob", limit=1, window_seconds=60)

    assert first_alice.allowed is True
    assert second_alice.allowed is False
    assert first_bob.allowed is True


def test_json_logging_redacts_secrets_and_free_form_content():
    context = {
        "request_id": "request-1",
        "authorization": "Bearer secret-token",
        "clientSecret": "another-secret",
        "email": "user@example.com",
        "tool_args": {"recipient": "target@example.com"},
        "image": "base64-private-image",
        "images": ["another-private-image"],
        "prompt": "private user prompt",
        "content": "private model output",
        "nested": {"password": "secret-password", "status": "ok"},
    }
    record = logging.LogRecord(
        "langgraph.api",
        logging.INFO,
        __file__,
        1,
        "request.completed Bearer visible-token user@example.com",
        (),
        None,
    )
    record.context = sanitize_for_log(context)
    payload = JsonLogFormatter().format(record)
    decoded = json.loads(payload)

    assert "secret-token" not in payload
    assert "secret-password" not in payload
    assert "another-secret" not in payload
    assert "visible-token" not in payload
    assert "target@example.com" not in payload
    assert "user@example.com" not in payload
    assert "base64-private-image" not in payload
    assert "another-private-image" not in payload
    assert "private user prompt" not in payload
    assert "private model output" not in payload
    assert decoded["authorization"] == "[REDACTED]"
    assert decoded["clientSecret"] == "[REDACTED]"
    assert decoded["tool_args"] == "[REDACTED]"
    assert decoded["nested"]["status"] == "ok"


def test_tool_policy_fails_closed_for_side_effect_registration():
    policy = ToolApprovalPolicy(frozenset({"send_email"}))

    decision = policy.evaluate("send_email")
    approved = policy.evaluate("send_email", approved=True)
    preview = policy.safe_preview(
        "send_email",
        {"recipient": "user@example.com", "body": "secret"},
    )

    assert decision.requires_approval is True
    assert decision.allowed is False
    assert approved.allowed is True
    assert preview == {
        "tool_name": "send_email",
        "arguments": {"recipient": "str", "body": "str"},
    }
    with pytest.raises(UnapprovedToolRegistrationError):
        policy.validate_registration(["calculator", "send_email"])


def test_agent_refuses_side_effect_tool_without_approval_executor():
    @tool("send_email")
    def send_email(recipient: str) -> str:
        """Send an external email."""
        return recipient

    with pytest.raises(UnapprovedToolRegistrationError):
        create_agent(
            tools=[send_email],
            chat_model=object(),
            settings=Settings(approval_required_tools="send_email"),
        )


def test_cors_rejects_full_wildcard_configuration():
    with pytest.raises(ValueError, match="具体的なOrigin"):
        make_test_app(settings_overrides={"cors_allowed_origins": "*"})
