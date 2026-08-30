"""OIDC authentication and per-user authorization tests."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.api.app import create_app
from src.api.auth import OpenIDConnectAuthenticator
from src.config import Settings
from tests.test_api import StubAgentService, StubModelService

ISSUER = "http://keycloak.test/realms/langgraph"
AUDIENCE = "langgraph-api"
KID = "test-signing-key"


def _base64url_uint(value: int) -> str:
    size = (value.bit_length() + 7) // 8
    return jwt.utils.base64url_encode(value.to_bytes(size, "big")).decode("ascii")


@pytest.fixture
def oidc_components() -> tuple[Settings, OpenIDConnectAuthenticator, Any]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    public_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "use": "sig",
                "alg": "RS256",
                "n": _base64url_uint(public_numbers.n),
                "e": _base64url_uint(public_numbers.e),
            }
        ]
    }

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/certs"},
            )
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    settings = Settings(
        auth_mode="oidc",
        oidc_issuer_url=ISSUER,
        oidc_audience=AUDIENCE,
        database_url=None,
        checkpoint_database_url=None,
    )
    authenticator = OpenIDConnectAuthenticator(
        settings,
        transport=httpx.MockTransport(provider),
    )
    return settings, authenticator, private_key


def _token(
    private_key: Any,
    subject: str,
    *,
    audience: str = AUDIENCE,
    expires_in: int = 300,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": audience,
            "sub": subject,
            "iat": now,
            "exp": now + expires_in,
            "preferred_username": subject,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": KID},
    )


def _app(settings: Settings, authenticator: OpenIDConnectAuthenticator):
    return create_app(
        settings=settings,
        authenticator=authenticator,
        agent_service=StubAgentService(),
        model_service=StubModelService(),
    )


@pytest.mark.asyncio
async def test_oidc_requires_bearer_token_but_keeps_health_public(oidc_components):
    settings, authenticator, _private_key = oidc_components
    transport = httpx.ASGITransport(app=_app(settings, authenticator))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        denied = await client.get("/v1/conversations")

    assert health.status_code == 200
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "authentication_required"
    assert denied.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_oidc_subjects_are_isolated_from_each_other(oidc_components):
    settings, authenticator, private_key = oidc_components
    transport = httpx.ASGITransport(app=_app(settings, authenticator))
    alice = {"Authorization": f"Bearer {_token(private_key, 'alice')}"}
    bob = {"Authorization": f"Bearer {_token(private_key, 'bob')}"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/conversations", headers=alice)
        conversation_id = created.json()["id"]
        alice_list = await client.get("/v1/conversations", headers=alice)
        bob_list = await client.get("/v1/conversations", headers=bob)
        bob_read = await client.get(
            f"/v1/conversations/{conversation_id}",
            headers=bob,
        )

    assert created.status_code == 201
    assert alice_list.json()["total"] == 1
    assert bob_list.json()["total"] == 0
    assert bob_read.status_code == 404
    assert bob_read.json()["error"]["code"] == "conversation_not_found"


@pytest.mark.asyncio
async def test_oidc_rejects_token_for_another_audience(oidc_components):
    settings, authenticator, private_key = oidc_components
    transport = httpx.ASGITransport(app=_app(settings, authenticator))
    headers = {
        "Authorization": f"Bearer {_token(private_key, 'alice', audience='other-api')}"
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/conversations", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_access_token"
    assert "invalid_token" in response.headers["WWW-Authenticate"]


@pytest.mark.asyncio
async def test_oidc_rejects_expired_token(oidc_components):
    settings, authenticator, private_key = oidc_components
    transport = httpx.ASGITransport(app=_app(settings, authenticator))
    headers = {
        "Authorization": f"Bearer {_token(private_key, 'alice', expires_in=-120)}"
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/conversations", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_access_token"
