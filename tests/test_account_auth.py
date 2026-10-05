"""Account OAuth port (Lane B): device flows, token store, refresh, bearer resolution.

Every HTTP call goes through ``httpx.MockTransport`` -- no network. ``Path.home``
is redirected to ``tmp_path`` so the real ``~/.config/screenscribe/accounts.json``
is never touched. ``sleep`` is injected so poll loops finish instantly.
"""

# ruff: noqa: S105, S106 -- fixture tokens are literal test strings, not credentials
from __future__ import annotations

import base64
import json
import os
import stat
import sys
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from screenscribe import account_auth
from screenscribe.account_auth import (
    OPENAI_CODEX_CLI_CLIENT_ID,
    XAI_GROK_CLI_CLIENT_ID,
    AccountAuthError,
    AccountTokens,
    account_status,
    accounts_path,
    clear_account_tokens,
    load_account_tokens,
    refresh_tokens,
    resolve_bearer,
    store_account_tokens,
)
from screenscribe.account_auth import device_code as device_code_module
from screenscribe.account_auth.device_code import (
    XAI_DEVICE_CODE_GRANT_TYPE,
    complete_device_code_login,
    request_device_code,
)
from screenscribe.account_auth.pkce import challenge_for_verifier, generate_pkce
from screenscribe.config import ScreenScribeConfig

OPENAI_ISSUER = "https://auth.openai.test"
XAI_ISSUER = "https://auth.xai.test"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SCREENSCRIBE_OPENAI_OAUTH_ISSUER", OPENAI_ISSUER)
    monkeypatch.setenv("SCREENSCRIBE_XAI_OAUTH_ISSUER", XAI_ISSUER)
    monkeypatch.delenv("SCREENSCRIBE_OPENAI_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_XAI_OAUTH_CLIENT_ID", raising=False)
    ScreenScribeConfig._account_warned.clear()


class Recorder:
    """MockTransport handler: scripted responses per (method, path) + call log."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.scripts: dict[str, list[httpx.Response]] = {}
        self.sleeps: list[float] = []

    def script(self, path: str, *responses: httpx.Response) -> None:
        self.scripts.setdefault(path, []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        queue = self.scripts.get(request.url.path)
        if not queue:
            return httpx.Response(599, json={"error": f"unscripted {request.url.path}"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def paths(self) -> list[str]:
        return [c.url.path for c in self.calls]


def _jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #


def test_pkce_challenge_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # pragma: allowlist secret
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"  # pragma: allowlist secret
    assert challenge_for_verifier(verifier) == expected


def test_generated_verifier_is_in_pkce_band_and_consistent() -> None:
    codes = generate_pkce()
    assert 43 <= len(codes.code_verifier) <= 128
    assert challenge_for_verifier(codes.code_verifier) == codes.code_challenge
    assert codes != generate_pkce()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_defaults_and_env_overrides(monkeypatch: Any) -> None:
    monkeypatch.delenv("SCREENSCRIBE_OPENAI_OAUTH_ISSUER")
    monkeypatch.delenv("SCREENSCRIBE_XAI_OAUTH_ISSUER")
    assert account_auth.issuer_for("openai") == "https://auth.openai.com"
    assert account_auth.issuer_for("xai") == "https://auth.x.ai"
    assert account_auth.client_id_for("openai") == OPENAI_CODEX_CLI_CLIENT_ID
    assert account_auth.client_id_for("xai") == XAI_GROK_CLI_CLIENT_ID
    monkeypatch.setenv("SCREENSCRIBE_XAI_OAUTH_ISSUER", "https://alt.example/ ")
    monkeypatch.setenv("SCREENSCRIBE_XAI_OAUTH_CLIENT_ID", " my-client ")
    assert account_auth.issuer_for("xai") == "https://alt.example"
    assert account_auth.client_id_for("xai") == "my-client"
    with pytest.raises(AccountAuthError) as excinfo:
        account_auth.provider_oauth_config("anthropic")
    assert excinfo.value.kind == "unsupported_provider"


def test_notice_discloses_shipped_client_ids() -> None:
    notice = (Path(__file__).resolve().parents[1] / "NOTICE").read_text(encoding="utf-8")
    assert OPENAI_CODEX_CLI_CLIENT_ID in notice
    assert XAI_GROK_CLI_CLIENT_ID in notice


# --------------------------------------------------------------------------- #
# OpenAI Codex device flow
# --------------------------------------------------------------------------- #


def test_openai_device_flow_pending_then_success_exchanges_with_pkce() -> None:
    rec = Recorder()
    rec.script(
        "/api/accounts/deviceauth/usercode",
        httpx.Response(
            200, json={"device_auth_id": "dev-1", "user_code": "USER-1", "interval": "3"}
        ),
    )
    rec.script(
        "/api/accounts/deviceauth/token",
        httpx.Response(403),
        httpx.Response(404),
        httpx.Response(
            200,
            json={
                "authorization_code": "auth-code",
                "code_verifier": "verifier",
                "code_challenge": "chal",
            },
        ),
    )
    rec.script(
        "/oauth/token",
        httpx.Response(
            200,
            json={
                "access_token": "at-openai",
                "refresh_token": "rt-openai",
                "id_token": _jwt({"email": "user@example.com"}),
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        ),
    )
    with rec.client() as client:
        device = request_device_code("openai", client=client)
        assert device.verification_url == f"{OPENAI_ISSUER}/codex/device"
        assert device.user_code == "USER-1"
        assert device.interval == 3
        usercode_req = rec.calls[0]
        assert json.loads(usercode_req.content) == {"client_id": OPENAI_CODEX_CLI_CLIENT_ID}

        tokens = complete_device_code_login(device, client=client, sleep=rec.sleep)

    assert tokens.access_token == "at-openai"
    assert rec.sleeps == [3, 3]
    assert rec.paths().count("/api/accounts/deviceauth/token") == 3
    poll_req = rec.calls[1]
    assert json.loads(poll_req.content) == {"device_auth_id": "dev-1", "user_code": "USER-1"}
    exchange = rec.calls[-1]
    assert exchange.headers["content-type"] == "application/x-www-form-urlencoded"
    assert _form(exchange) == {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "redirect_uri": f"{OPENAI_ISSUER}/deviceauth/callback",
        "client_id": OPENAI_CODEX_CLI_CLIENT_ID,
        "code_verifier": "verifier",
    }
    status = account_status("openai")
    assert status.signed_in and status.email == "user@example.com"
    assert status.api_bearer_usable is False


def test_openai_poll_non_pending_status_is_terminal() -> None:
    rec = Recorder()
    rec.script(
        "/api/accounts/deviceauth/usercode",
        httpx.Response(200, json={"device_auth_id": "d", "user_code": "U"}),
    )
    rec.script("/api/accounts/deviceauth/token", httpx.Response(500))
    with rec.client() as client:
        device = request_device_code("openai", client=client)
        assert device.interval == 5  # missing interval -> default, never a hot loop
        with pytest.raises(AccountAuthError) as excinfo:
            complete_device_code_login(device, client=client, sleep=rec.sleep)
    assert excinfo.value.kind == "oauth"
    assert not accounts_path().exists()


def test_openai_poll_times_out_within_budget() -> None:
    rec = Recorder()
    rec.script(
        "/api/accounts/deviceauth/usercode",
        httpx.Response(200, json={"device_auth_id": "d", "user_code": "U"}),
    )
    rec.script("/api/accounts/deviceauth/token", httpx.Response(403))
    with rec.client() as client:
        device = request_device_code("openai", client=client)
        with pytest.raises(AccountAuthError, match="timed out"):
            complete_device_code_login(device, client=client, sleep=rec.sleep, max_wait=0)
    assert rec.sleeps == []


# --------------------------------------------------------------------------- #
# xAI RFC 8628 device flow
# --------------------------------------------------------------------------- #


def _xai_device_response(**extra: Any) -> httpx.Response:
    body = {
        "device_code": "dc-1",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://auth.x.ai/device",
        "interval": 5,
        "expires_in": 600,
    }
    body.update(extra)
    return httpx.Response(200, json=body)


def _xai_token_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "at-xai",
            "refresh_token": "rt-xai",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )


def test_xai_device_flow_happy_path_stores_tokens_with_0600() -> None:
    rec = Recorder()
    rec.script(
        "/oauth2/device/code",
        _xai_device_response(verification_uri_complete="https://auth.x.ai/device?c=ABCD"),
    )
    rec.script("/oauth2/token", _xai_token_ok())
    with rec.client() as client:
        device = request_device_code("xai", client=client)
        assert device.verification_url == "https://auth.x.ai/device?c=ABCD"
        assert device.user_code == "ABCD-EFGH"
        assert device.device_auth_id == "dc-1"
        req = rec.calls[0]
        assert req.headers["content-type"] == "application/x-www-form-urlencoded"
        assert _form(req) == {
            "client_id": XAI_GROK_CLI_CLIENT_ID,
            "scope": "openid profile email offline_access grok-cli:access api:access",
            "referrer": "screenscribe",
        }
        # scope is %20-escaped, never '+'
        assert b"scope=openid%20profile" in req.content
        tokens = complete_device_code_login(device, client=client, sleep=rec.sleep)

    assert tokens.access_token == "at-xai"
    assert rec.sleeps == []
    poll = rec.calls[1]
    assert _form(poll) == {
        "grant_type": XAI_DEVICE_CODE_GRANT_TYPE,
        "client_id": XAI_GROK_CLI_CLIENT_ID,
        "device_code": "dc-1",
    }
    path = accounts_path()
    assert path == Path.home() / ".config" / "screenscribe" / "accounts.json"
    if os.name == "posix" and sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    stored = json.loads(path.read_text())["accounts"]["xai"]
    assert stored["access_token"] == "at-xai"
    assert stored["refresh_token"] == "rt-xai"
    assert isinstance(stored["expires_at"], int)
    assert load_account_tokens("xai").access_token == "at-xai"


def test_xai_poll_pending_then_slow_down_then_success() -> None:
    rec = Recorder()
    rec.script("/oauth2/device/code", _xai_device_response(interval=1))  # floor to 5 s
    rec.script(
        "/oauth2/token",
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(400, json={"error": "authorization_pending"}),
        _xai_token_ok(),
    )
    with rec.client() as client:
        device = request_device_code("xai", client=client)
        assert device.interval == 5
        complete_device_code_login(device, client=client, sleep=rec.sleep)
    assert rec.sleeps == [5, 10, 10]
    assert rec.paths().count("/oauth2/token") == 4


@pytest.mark.parametrize(
    ("error", "match", "kind"),
    [
        ("access_denied", "denied", "oauth"),
        ("expired_token", "expired", "oauth"),
        ("invalid_grant", "invalid_grant", "oauth"),
    ],
)
def test_xai_poll_terminal_errors(error: str, match: str, kind: str) -> None:
    rec = Recorder()
    rec.script("/oauth2/device/code", _xai_device_response())
    rec.script("/oauth2/token", httpx.Response(400, json={"error": error}))
    with rec.client() as client:
        device = request_device_code("xai", client=client)
        with pytest.raises(AccountAuthError, match=match) as excinfo:
            complete_device_code_login(device, client=client, sleep=rec.sleep)
    assert excinfo.value.kind == kind
    assert not accounts_path().exists()


def test_xai_device_code_missing_fields_is_refused() -> None:
    rec = Recorder()
    rec.script("/oauth2/device/code", httpx.Response(200, json={"user_code": "X"}))
    with rec.client() as client, pytest.raises(AccountAuthError, match="missing device_code"):
        request_device_code("xai", client=client)


# --------------------------------------------------------------------------- #
# Store / status / logout
# --------------------------------------------------------------------------- #


def test_store_is_per_provider_and_logout_removes_one_slot() -> None:
    store_account_tokens("xai", AccountTokens(access_token="a-xai", expires_at=None))
    store_account_tokens(
        "openai", AccountTokens(access_token="a-openai", id_token=_jwt({"sub": "user-1"}))
    )
    assert load_account_tokens("xai").access_token == "a-xai"
    assert account_status("openai").email == "user-1"
    assert clear_account_tokens("xai") is True
    assert clear_account_tokens("xai") is False
    with pytest.raises(AccountAuthError) as excinfo:
        load_account_tokens("xai")
    assert excinfo.value.kind == "not_signed_in"
    assert load_account_tokens("openai").access_token == "a-openai"
    status = account_status("xai")
    assert status.signed_in is False and status.email is None


def test_tokens_repr_never_contains_secret() -> None:
    tokens = AccountTokens(access_token="secret-access", refresh_token="secret-refresh")
    assert "secret" not in repr(tokens)
    assert "secret" not in str(tokens)


def test_status_marks_expired_and_corrupt_store_is_storage_error() -> None:
    store_account_tokens("xai", AccountTokens(access_token="old", expires_at=1))
    assert account_status("xai").expired is True
    accounts_path().write_text("{not json")
    with pytest.raises(AccountAuthError) as excinfo:
        account_status("xai")
    assert excinfo.value.kind == "storage"


# --------------------------------------------------------------------------- #
# Refresh + resolve_bearer
# --------------------------------------------------------------------------- #


def test_refresh_uses_form_grant_and_carries_refresh_token_forward() -> None:
    rec = Recorder()
    rec.script(
        "/oauth2/token", httpx.Response(200, json={"access_token": "at-new", "expires_in": 60})
    )
    stale = AccountTokens(
        access_token="at-old", refresh_token="rt-1", id_token=_jwt({"email": "e@x"}), expires_at=1
    )
    store_account_tokens("xai", stale)
    with rec.client() as client:
        fresh = refresh_tokens("xai", stale, client=client)
    assert _form(rec.calls[0]) == {
        "grant_type": "refresh_token",
        "client_id": XAI_GROK_CLI_CLIENT_ID,
        "refresh_token": "rt-1",
    }
    assert rec.calls[0].url == f"{XAI_ISSUER}/oauth2/token"
    assert fresh.access_token == "at-new"
    assert fresh.refresh_token == "rt-1"  # provider did not rotate -> carried forward
    assert load_account_tokens("xai").access_token == "at-new"
    assert account_status("xai").email == "e@x"  # id_token preserved for display


def test_resolve_bearer_explicit_key_wins_without_touching_store() -> None:
    accounts_path().parent.mkdir(parents=True)
    accounts_path().write_text("{not json")  # would raise storage error if read
    resolution = resolve_bearer("xai", "sk-explicit")
    assert (
        resolution.bearer == "sk-explicit" and resolution.source == "explicit" and resolution.usable
    )


def test_resolve_bearer_xai_account_token_refreshes_when_expiring() -> None:
    rec = Recorder()
    rec.script(
        "/oauth2/token", httpx.Response(200, json={"access_token": "at-fresh", "expires_in": 3600})
    )
    store_account_tokens(
        "xai", AccountTokens(access_token="at-stale", refresh_token="rt", expires_at=1)
    )
    with rec.client() as client:
        resolution = resolve_bearer("xai", "", client=client)
    assert resolution.usable and resolution.bearer == "at-fresh" and resolution.source == "account"
    assert rec.paths() == ["/oauth2/token"]


def test_resolve_bearer_expired_without_refresh_token_is_none_with_warning() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-stale", expires_at=1))
    resolution = resolve_bearer("xai", "")
    assert resolution.bearer == "" and resolution.source == "none"
    assert resolution.warning and "auth login xai" in resolution.warning


def test_resolve_bearer_not_signed_in_is_none() -> None:
    resolution = resolve_bearer("xai", "")
    assert resolution.bearer == "" and resolution.source == "none" and resolution.warning is None


def test_resolve_bearer_openai_is_identity_only_with_warning() -> None:
    store_account_tokens("openai", AccountTokens(access_token="chatgpt-token", expires_at=None))
    resolution = resolve_bearer("openai", "")
    assert resolution.bearer == "chatgpt-token"
    assert resolution.identity_only is True and resolution.usable is False
    assert resolution.warning and "2026-09-08" in resolution.warning


# --------------------------------------------------------------------------- #
# Config integration
# --------------------------------------------------------------------------- #


def test_config_getters_fall_back_to_xai_account_only_on_api_x_ai() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-xai"))
    config = ScreenScribeConfig(
        stt_endpoint="https://api.x.ai/v1/stt",
        llm_endpoint="https://api.x.ai/v1/responses",
        vision_endpoint="https://api.libraxis.cloud/v1/responses",
    )
    assert config.get_stt_api_key() == "at-xai"
    assert config.get_llm_api_key() == "at-xai"
    assert config.get_vision_api_key() == ""  # libraxis host never receives an account token
    config.stt_api_key = "sk-explicit"  # pragma: allowlist secret
    assert config.get_stt_api_key() == "sk-explicit"
    config.api_key = "generic"  # pragma: allowlist secret
    assert config.get_vision_api_key() == "generic"


@pytest.mark.parametrize("service", ["tts", "stt_live"])
@pytest.mark.parametrize("destination", ["xai", "custom", "explicit"])
def test_account_bearer_follows_actual_auxiliary_service_destination(
    service: str, destination: str
) -> None:
    store_account_tokens("xai", AccountTokens.from_token_response({"access_token": "test-account"}))
    config = ScreenScribeConfig.provider_preset("xai", "")
    if destination != "xai":
        if service == "tts":
            config.tts_endpoint = "https://other.example/v1/tts"
        else:
            config.stt_live_endpoint = "wss://other.example/stt"
    if destination == "explicit":
        if service == "tts":
            config.tts_api_key = "explicit-fixture"  # pragma: allowlist secret
        else:
            config.stt_api_key = "explicit-fixture"  # pragma: allowlist secret
    credential = getattr(config, f"get_{service}_api_key")()
    assert (
        credential
        == {
            "xai": "test-account",
            "custom": "",
            "explicit": "explicit-fixture",
        }[destination]
    )


def test_config_openai_account_warns_once_and_returns_no_key() -> None:
    store_account_tokens("openai", AccountTokens(access_token="chatgpt-token"))
    config = ScreenScribeConfig.provider_preset("openai", "")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert config.get_llm_api_key() == ""
        assert config.get_stt_api_key() == ""
    identity = [w for w in caught if "identity-only" in str(w.message)]
    assert len(identity) == 1
    assert "chatgpt-token" not in str(identity[0].message)
    # An explicit key still wins and silences the fallback entirely.
    config.llm_api_key = "sk-real"  # pragma: allowlist secret
    assert config.get_llm_api_key() == "sk-real"


def test_config_default_libraxis_never_reads_account_store() -> None:
    accounts_path().parent.mkdir(parents=True)
    accounts_path().write_text("{not json")
    config = ScreenScribeConfig()
    assert config.get_stt_api_key() == ""
    assert config.configuration_status() == "INCOMPLETE - API key required"


def test_device_code_transport_error_redacts_override_url() -> None:
    url = (
        "https://operator:password@auth.example.test/oauth2/device/code"  # pragma: allowlist secret
        "?key=supersecret&tenant=clinic"
    )

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not connect to {request.url}", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(AccountAuthError) as exc_info:
            device_code_module._post(client, url)

    message = str(exc_info.value)
    assert exc_info.value.kind == "http"
    assert "ConnectError" in message
    assert "auth.example.test/oauth2/device/code" in message
    assert "operator" not in message
    assert "password" not in message
    assert "supersecret" not in message
    assert "clinic" not in message
    assert "key=***" in message
    assert "tenant=***" in message
