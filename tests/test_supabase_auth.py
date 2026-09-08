"""Supabase Auth client tests."""

from unittest.mock import patch

import httpx
import pytest

from src.supabase_auth import SupabaseAuthClient, SupabaseAuthError


def _response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "http://supabase.test/auth/v1/token"),
    )


def test_supabase_auth_sign_in_refresh_and_logout():
    client = SupabaseAuthClient("http://supabase.test", "publishable", 5)
    login = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": 2_000_000_000,
        "user": {"email": "user@example.com"},
    }
    refreshed = {
        "access_token": "access-2",
        "refresh_token": "refresh-2",
        "expires_at": 2_000_000_100,
    }

    with patch(
        "src.supabase_auth.httpx.post",
        side_effect=[_response(200, login), _response(200, refreshed), _response(204)],
    ) as post:
        session = client.sign_in(" user@example.com ", "password")
        session = client.refresh(session.refresh_token)
        client.logout(session.access_token)

    assert session.access_token == "access-2"
    assert post.call_args_list[0].kwargs["json"]["email"] == "user@example.com"
    assert post.call_args_list[2].kwargs["headers"]["Authorization"] == "Bearer access-2"


def test_supabase_auth_hides_rejected_response_details():
    client = SupabaseAuthClient("http://supabase.test", "publishable", 5)

    with patch("src.supabase_auth.httpx.post", return_value=_response(400)):
        with pytest.raises(SupabaseAuthError, match="メールアドレスまたはパスワード"):
            client.sign_in("user@example.com", "wrong-password")
