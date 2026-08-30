"""OpenID Connect access-token validation for API clients."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from src.config import Settings


logger = logging.getLogger("langgraph.api.auth")


class AuthenticationError(RuntimeError):
    """A bearer token is missing or cannot be trusted."""


class AuthenticationUnavailable(RuntimeError):
    """The identity provider cannot currently be reached."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Identity claims needed by the application domain."""

    subject: str
    display_name: str | None = None


class OpenIDConnectAuthenticator:
    """Validate RS256 tokens using an OIDC provider's rotating JWKS."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        issuer = (settings.oidc_issuer_url or "").rstrip("/")
        if settings.auth_mode == "oidc" and not issuer:
            raise ValueError("AUTH_MODE=oidcではOIDC_ISSUER_URLが必要です")
        self.issuer = issuer
        self.audience = settings.oidc_audience
        self.jwks_url = settings.oidc_jwks_url
        self.cache_seconds = settings.oidc_jwks_cache_seconds
        self.timeout_seconds = settings.oidc_http_timeout_seconds
        self.clock_skew_seconds = settings.oidc_clock_skew_seconds
        self.transport = transport
        self._keys: dict[str, Any] = {}
        self._cache_deadline = 0.0
        self._lock = asyncio.Lock()

    async def authenticate(self, token: str) -> AuthenticatedPrincipal:
        """Verify a bearer access token and return its stable subject."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise AuthenticationError("アクセストークンが不正です。") from error

        if header.get("alg") != "RS256":
            raise AuthenticationError("アクセストークンが不正です。")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationError("アクセストークンが不正です。")

        key = await self._signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.clock_skew_seconds,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as error:
            logger.warning(
                "access_token.validation_failed.%s",
                type(error).__name__,
            )
            raise AuthenticationError("アクセストークンが不正です。") from error

        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject.strip()) > 255
        ):
            raise AuthenticationError("アクセストークンが不正です。")
        display_name = next(
            (
                value.strip()
                for claim in ("preferred_username", "name", "email")
                if isinstance((value := claims.get(claim)), str) and value.strip()
            ),
            None,
        )
        return AuthenticatedPrincipal(
            subject=subject.strip(),
            display_name=display_name[:200] if display_name else None,
        )

    async def _signing_key(self, kid: str) -> Any:
        now = time.monotonic()
        if now < self._cache_deadline and kid in self._keys:
            return self._keys[kid]

        async with self._lock:
            now = time.monotonic()
            if now >= self._cache_deadline or kid not in self._keys:
                await self._refresh_keys()
            key = self._keys.get(kid)
            if key is None:
                raise AuthenticationError("アクセストークンの署名鍵が見つかりません。")
            return key

    async def _refresh_keys(self) -> None:
        try:
            jwks_url = self.jwks_url or await self._discover_jwks_url()
            payload = await self._get_json(jwks_url)
            keys = payload.get("keys")
            if not isinstance(keys, list):
                raise ValueError("JWKSにkeysがありません")
            parsed: dict[str, Any] = {}
            for item in keys:
                if not isinstance(item, dict):
                    continue
                kid = item.get("kid")
                if not isinstance(kid, str) or not kid:
                    continue
                parsed[kid] = jwt.PyJWK.from_dict(item, algorithm="RS256").key
            if not parsed:
                raise ValueError("利用可能な署名鍵がありません")
        except AuthenticationUnavailable:
            raise
        except Exception as error:
            raise AuthenticationUnavailable(
                "認証サーバーから署名鍵を取得できません。"
            ) from error

        self._keys = parsed
        self._cache_deadline = time.monotonic() + self.cache_seconds

    async def _discover_jwks_url(self) -> str:
        payload = await self._get_json(
            f"{self.issuer}/.well-known/openid-configuration"
        )
        discovered_issuer = payload.get("issuer")
        jwks_url = payload.get("jwks_uri")
        if discovered_issuer != self.issuer or not isinstance(jwks_url, str):
            raise AuthenticationUnavailable("OIDC Discovery情報が不正です。")
        return jwks_url

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AuthenticationUnavailable(
                "認証サーバーへ接続できません。"
            ) from error
        if not isinstance(payload, dict):
            raise AuthenticationUnavailable("認証サーバーの応答が不正です。")
        return payload
