"""Small Supabase Auth client for the Streamlit server."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class SupabaseAuthError(RuntimeError):
    """Supabase Auth rejected the request or could not be reached."""


@dataclass(frozen=True, slots=True)
class SupabaseSession:
    """Per-user credentials stored only in Streamlit session state."""

    access_token: str
    refresh_token: str
    expires_at: int
    email: str | None = None


class SupabaseAuthClient:
    """Call the three GoTrue endpoints needed by the Streamlit UI."""

    def __init__(self, url: str, publishable_key: str, timeout_seconds: float) -> None:
        self.auth_url = f"{url.rstrip('/')}/auth/v1"
        self.headers = {"apikey": publishable_key}
        self.timeout_seconds = timeout_seconds

    def sign_in(self, email: str, password: str) -> SupabaseSession:
        return self._session_request(
            "password",
            {"email": email.strip(), "password": password},
            "メールアドレスまたはパスワードを確認してください。",
        )

    def refresh(self, refresh_token: str) -> SupabaseSession:
        return self._session_request(
            "refresh_token",
            {"refresh_token": refresh_token},
            "セッションを更新できませんでした。再ログインしてください。",
        )

    def logout(self, access_token: str) -> None:
        try:
            response = httpx.post(
                f"{self.auth_url}/logout",
                params={"scope": "local"},
                headers={**self.headers, "Authorization": f"Bearer {access_token}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise SupabaseAuthError("ログアウトを完了できませんでした。") from error

    def _session_request(
        self,
        grant_type: str,
        body: dict[str, str],
        rejected_message: str,
    ) -> SupabaseSession:
        try:
            response = httpx.post(
                f"{self.auth_url}/token",
                params={"grant_type": grant_type},
                headers=self.headers,
                json=body,
                timeout=self.timeout_seconds,
            )
            if 400 <= response.status_code < 500:
                raise SupabaseAuthError(rejected_message)
            response.raise_for_status()
            payload = response.json()
        except SupabaseAuthError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise SupabaseAuthError("認証サーバーへ接続できませんでした。") from error
        return self._parse_session(payload)

    @staticmethod
    def _parse_session(payload: Any) -> SupabaseSession:
        if not isinstance(payload, dict):
            raise SupabaseAuthError("認証サーバーの応答が不正です。")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, int):
            expires_in = payload.get("expires_in")
            if not isinstance(expires_in, int):
                raise SupabaseAuthError("認証サーバーの応答が不正です。")
            expires_at = int(time.time()) + expires_in
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
        ):
            raise SupabaseAuthError("認証サーバーの応答が不正です。")
        user = payload.get("user")
        email = user.get("email") if isinstance(user, dict) else None
        return SupabaseSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            email=email if isinstance(email, str) else None,
        )
