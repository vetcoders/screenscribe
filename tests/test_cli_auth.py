"""CLI contract for ``screenscribe auth login|status|logout``. No network, no browser."""

# ruff: noqa: S105, S106 -- fixture tokens are literal test strings, not credentials
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from screenscribe import cli_auth
from screenscribe.account_auth import AccountTokens, accounts_path, store_account_tokens
from screenscribe.account_auth import device_code as device_code_mod
from screenscribe.cli import app

_RealClient = httpx.Client

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return " ".join(_ANSI_RE.sub("", output).split())


def _jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def _isolate(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SCREENSCRIBE_XAI_OAUTH_ISSUER", "https://auth.xai.test")
    monkeypatch.setenv("SCREENSCRIBE_OPENAI_OAUTH_ISSUER", "https://auth.openai.test")


def test_auth_login_xai_prints_url_and_code_and_stores_tokens(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _isolate(tmp_path, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(cli_auth.webbrowser, "open", lambda url: opened.append(url) or True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "WXYZ-1234",
                    "verification_uri": "https://auth.x.ai/device",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "secret-at",
                "refresh_token": "secret-rt",
                "id_token": _jwt({"email": "me@x.ai"}),
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(
        device_code_mod.httpx,
        "Client",
        lambda *a, **k: _RealClient(transport=httpx.MockTransport(handler)),
    )

    result = CliRunner().invoke(app, ["auth", "login", "xai"])
    out = _plain(result.output)
    assert result.exit_code == 0, out
    assert "https://auth.x.ai/device" in out
    assert "WXYZ-1234" in out
    assert "Signed in to xai as me@x.ai" in out
    assert "secret-at" not in out and "secret-rt" not in out
    assert opened == ["https://auth.x.ai/device"]
    assert json.loads(accounts_path().read_text())["accounts"]["xai"]["access_token"] == "secret-at"


def test_auth_login_no_browser_and_openai_identity_note(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_auth.webbrowser,
        "open",
        lambda url: (_ for _ in ()).throw(AssertionError("browser opened")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/accounts/deviceauth/usercode":
            return httpx.Response(
                200, json={"device_auth_id": "d", "user_code": "CODE-1", "interval": 1}
            )
        if path == "/api/accounts/deviceauth/token":
            return httpx.Response(
                200, json={"authorization_code": "c", "code_verifier": "v", "code_challenge": "x"}
            )
        return httpx.Response(200, json={"access_token": "secret", "expires_in": 10})

    monkeypatch.setattr(
        device_code_mod.httpx,
        "Client",
        lambda *a, **k: _RealClient(transport=httpx.MockTransport(handler)),
    )

    result = CliRunner().invoke(app, ["auth", "login", "openai", "--no-browser"])
    out = _plain(result.output)
    assert result.exit_code == 0, out
    assert "https://auth.openai.test/codex/device" in out and "CODE-1" in out
    assert "identity-only" in out
    assert "secret" not in out


def test_auth_status_and_logout(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    out = _plain(runner.invoke(app, ["auth", "status"]).output)
    assert "openai not signed in" in out and "xai not signed in" in out

    store_account_tokens(
        "xai",
        AccountTokens(
            access_token="tok-secret",
            refresh_token="r",
            id_token=_jwt({"email": "a@b"}),
            expires_at=2_000_000_000,
        ),
    )
    result = runner.invoke(app, ["auth", "status"])
    out = _plain(result.output)
    assert result.exit_code == 0
    assert (
        "xai signed in as a@b" in out
        and "usable as API bearer" in out
        and "refresh token stored" in out
    )
    assert "tok-secret" not in out

    assert runner.invoke(app, ["auth", "logout", "xai"]).exit_code == 0
    assert "xai not signed in" in _plain(runner.invoke(app, ["auth", "status"]).output)
    assert "no stored session" in _plain(runner.invoke(app, ["auth", "logout", "xai"]).output)


def test_auth_unknown_provider_exits_2(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["auth", "login", "anthropic"])
    assert result.exit_code == 2
    assert "unknown provider" in _plain(result.output)
