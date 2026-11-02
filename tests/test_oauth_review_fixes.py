"""Regressions from the adversarial review of the durable OAuth + ChatGPT route.

Each test pins one finding: a late refresh never undoes a sign-out, dead
OpenAI grants are recognised by their nested code, a read-only store refuses
before spending a refresh token, an identity-only host never refreshes, the
Codex transport keeps the environment proxy and never swaps the stored account
token in for a caller's own key, and a ChatGPT selection never writes
environment-only secrets to ``config.env``.

No network: every HTTP call goes through ``httpx.MockTransport``. ``Path.home``
is redirected to ``tmp_path`` so the real account store is never touched.
"""

# ruff: noqa: S105, S106 -- fixture tokens are literal test strings, not credentials
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from typer.testing import CliRunner

from screenscribe import account_auth
from screenscribe.account_auth import (
    AccountAuthError,
    AccountTokens,
    accounts_path,
    clear_account_tokens,
    load_account_tokens,
    refresh_tokens,
    resolve_bearer,
    store_account_tokens,
)
from screenscribe.api_utils import provider_client_options
from screenscribe.cli import app
from screenscribe.config import ScreenScribeConfig
from screenscribe.providers import chatgpt
from screenscribe.providers.chatgpt import (
    CHATGPT_RESPONSES_ENDPOINT,
    JWT_AUTH_CLAIM,
    CodexTransport,
)

OPENAI_ISSUER = "https://auth.openai.test"
XAI_ISSUER = "https://auth.xai.test"
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
POSIX_NON_ROOT = sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() != 0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SCREENSCRIBE_OPENAI_OAUTH_ISSUER", OPENAI_ISSUER)
    monkeypatch.setenv("SCREENSCRIBE_XAI_OAUTH_ISSUER", XAI_ISSUER)
    monkeypatch.delenv("SCREENSCRIBE_OPENAI_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_XAI_OAUTH_CLIENT_ID", raising=False)
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    ScreenScribeConfig._account_warned.clear()


class Recorder:
    """MockTransport handler: scripted responses in order + call log."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.calls: list[httpx.Request] = []
        self.queue = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.queue:
            return httpx.Response(599, json={"error": "unscripted"})
        return self.queue.pop(0)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _account_token(account_id: str = "acct-review-1", nonce: str = "a") -> str:
    claims = {JWT_AUTH_CLAIM: {"chatgpt_account_id": account_id}, "nonce": nonce}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def _accounts() -> dict[str, Any]:
    if not accounts_path().exists():
        return {}
    accounts: dict[str, Any] = json.loads(accounts_path().read_text(encoding="utf-8"))["accounts"]
    return accounts


# --------------------------------------------------------------------------- #
# 1. A refresh that races a sign-out never writes the account back
# --------------------------------------------------------------------------- #


def test_refresh_after_a_concurrent_sign_out_does_not_resurrect_the_account() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-old", refresh_token="rt-old"))
    loaded = load_account_tokens("xai")
    assert clear_account_tokens("xai") is True  # another process signs out
    rec = Recorder(httpx.Response(200, json={"access_token": "at-new", "expires_in": 3600}))
    with rec.client() as client:
        refreshed = refresh_tokens("xai", loaded, client=client)
    assert refreshed.access_token == "at-new"
    assert len(rec.calls) == 1
    assert "xai" not in _accounts()


def test_revoked_refresh_after_a_sign_out_writes_no_disabled_entry() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-old", refresh_token="rt-old"))
    loaded = load_account_tokens("xai")
    clear_account_tokens("xai")
    rec = Recorder(httpx.Response(400, json={"error": "invalid_grant"}))
    with rec.client() as client, pytest.raises(AccountAuthError) as excinfo:
        refresh_tokens("xai", loaded, client=client)
    assert excinfo.value.kind == "revoked"
    assert "xai" not in _accounts()


# --------------------------------------------------------------------------- #
# 2. Classification: any 401 from the token endpoint ends the grant
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [{"error": "invalid_client"}, {"error": {"code": "token_family_gone", "message": "x"}}],
    ids=["unrecognised-flat-code", "unrecognised-nested-code"],
)
def test_any_401_from_the_token_endpoint_is_definitive(body: dict[str, Any]) -> None:
    error = account_auth._classify_refresh_failure(httpx.Response(401, json=body))
    assert error.kind == "revoked"


def test_disabled_cause_keeps_only_a_plain_error_code() -> None:
    hostile = {"error": "[link=https://evil.example]sign in[/link]"}
    error = account_auth._classify_refresh_failure(httpx.Response(401, json=hostile))
    assert error.kind == "revoked"
    assert "[" not in str(error) and "/" not in str(error) and ":/" not in str(error)


# --------------------------------------------------------------------------- #
# 3. Store lock + filesystem failures
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform == "win32", reason="flock store lock is POSIX-only")
def test_store_lock_default_timeout_is_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    store_account_tokens("xai", AccountTokens(access_token="at"))
    lock = accounts_path().parent / account_auth.ACCOUNTS_LOCK_NAME
    holder = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)  # a second open description conflicts
        monkeypatch.setattr(account_auth, "STORE_LOCK_TIMEOUT_SECONDS", 0.1)
        started = time.monotonic()
        with pytest.raises(AccountAuthError) as excinfo:
            store_account_tokens("xai", AccountTokens(access_token="at-2"))
        assert excinfo.value.kind == "storage"
        assert time.monotonic() - started < 5.0
    finally:
        os.close(holder)


def test_store_write_failure_is_a_storage_error_not_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read-only mount (EROFS) or a full disk: the owner cannot chmod past it.
    store_account_tokens("xai", AccountTokens(access_token="at"))

    def read_only(*args: Any, **kwargs: Any) -> Any:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(account_auth.tempfile, "mkstemp", read_only)
    with pytest.raises(AccountAuthError) as excinfo:
        store_account_tokens("openai", AccountTokens(access_token="at-openai"))
    assert excinfo.value.kind == "storage"
    assert "Read-only file system" in str(excinfo.value)


def test_unwritable_store_refuses_refresh_before_spending_the_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = AccountTokens(access_token="at-old", refresh_token="rt-single-use", expires_at=1)
    store_account_tokens("xai", tokens)
    monkeypatch.setattr(account_auth, "_store_writable", lambda: False)
    rec = Recorder(httpx.Response(200, json={"access_token": "at-new"}))
    with rec.client() as client, pytest.raises(AccountAuthError) as excinfo:
        refresh_tokens("xai", tokens, client=client)
    assert excinfo.value.kind == "storage"
    assert rec.calls == []  # the single-use refresh token was never sent
    assert load_account_tokens("xai").refresh_token == "rt-single-use"


@pytest.mark.skipif(not POSIX_NON_ROOT, reason="needs POSIX permissions and a non-root user")
def test_store_writable_reflects_the_directory_permissions(tmp_path: Path) -> None:
    store_account_tokens("xai", AccountTokens(access_token="at"))
    assert account_auth._store_writable() is True
    directory = accounts_path().parent
    directory.chmod(0o500)
    try:
        assert account_auth._store_writable() is False
    finally:
        directory.chmod(0o700)


# --------------------------------------------------------------------------- #
# 4. An identity-only host never spends a refresh
# --------------------------------------------------------------------------- #


def test_identity_only_host_never_refreshes() -> None:
    store_account_tokens(
        "openai", AccountTokens(access_token="at-openai", refresh_token="rt", expires_at=1)
    )
    rec = Recorder(httpx.Response(200, json={"access_token": "at-new"}))
    with rec.client() as client:
        resolution = resolve_bearer("openai", "", host="api.openai.com", client=client)
    assert resolution.identity_only is True and resolution.usable is False
    assert rec.calls == []
    assert load_account_tokens("openai").refresh_token == "rt"


# --------------------------------------------------------------------------- #
# 5. Codex transport: own-key 401s, response cleanup, environment proxy
# --------------------------------------------------------------------------- #


def _body() -> dict[str, Any]:
    return {"model": "m", "input": "hi", "stream": False}


def test_401_for_a_caller_key_is_not_retried_with_the_stored_account_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account_token()
    store_account_tokens("openai", AccountTokens(access_token=account, refresh_token="rt"))
    refreshes: list[str] = []
    monkeypatch.setattr(
        account_auth, "force_refresh", lambda provider, rejected: refreshes.append(rejected)
    )
    upstream = Recorder(httpx.Response(401, json={"error": "bad key"}))
    transport = CodexTransport(inner=httpx.MockTransport(upstream))
    with httpx.Client(transport=transport) as client:
        response = client.post(
            CHATGPT_RESPONSES_ENDPOINT,
            json=_body(),
            headers={"Authorization": "Bearer sk-caller-chosen-key"},
        )
    assert response.status_code == 401
    assert refreshes == []
    assert len(upstream.calls) == 1
    assert account not in upstream.calls[0].headers.get("authorization", "")


class _TrackedStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Any:
        yield b'{"error": "expired"}'

    def close(self) -> None:
        self.closed = True


def test_401_response_is_closed_when_the_refresh_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _TrackedStream()

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=stream)

    def boom(rejected: str) -> str | None:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(chatgpt, "_refreshed_token", boom)
    transport = CodexTransport(inner=httpx.MockTransport(upstream))
    with httpx.Client(transport=transport) as client, pytest.raises(RuntimeError):
        client.post(
            CHATGPT_RESPONSES_ENDPOINT,
            json=_body(),
            headers={"Authorization": f"Bearer {_account_token()}"},
        )
    assert stream.closed is True


def test_codex_mount_keeps_the_environment_https_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    assert chatgpt._environment_proxy() == "http://127.0.0.1:9"
    options = provider_client_options(CHATGPT_RESPONSES_ENDPOINT)
    transport = options["mounts"]["https://chatgpt.com"]
    assert isinstance(transport, CodexTransport)
    assert isinstance(transport._inner, httpx.HTTPTransport)
    assert isinstance(transport._inner._pool, httpcore.HTTPProxy)


def test_codex_mount_honours_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "chatgpt.com")
    assert chatgpt._environment_proxy() is None
    transport = provider_client_options(CHATGPT_RESPONSES_ENDPOINT)["mounts"]["https://chatgpt.com"]
    assert isinstance(transport._inner, httpx.HTTPTransport)
    assert not isinstance(transport._inner._pool, httpcore.HTTPProxy)


# --------------------------------------------------------------------------- #
# 6. ChatGPT selection writes only what the saved file already held
# --------------------------------------------------------------------------- #


def _signed_in_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        account_auth,
        "account_status",
        lambda provider: account_auth.AccountStatus(
            provider=provider,
            signed_in=True,
            expires_at=None,
            email="u@example.test",
            api_bearer_usable=True,
            has_refresh_token=True,
            bearer_hosts=("chatgpt.com",),
        ),
    )


def test_chatgpt_selection_never_persists_environment_only_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_env = tmp_path / ".config" / "screenscribe" / "config.env"
    config_env.parent.mkdir(parents=True)
    config_env.write_text(
        "SCREENSCRIBE_STT_ENDPOINT=https://api.libraxis.cloud/v1/audio/transcriptions\n",
        encoding="utf-8",
    )
    config_env.chmod(0o600)
    monkeypatch.setattr("screenscribe.config.CONFIG_PATHS", [config_env])
    monkeypatch.setenv("LIBRAXIS_API_KEY", "test-libraxis-env-only")
    monkeypatch.setenv("SCREENSCRIBE_TTS_API_KEY", "xai-env-only-tts")
    monkeypatch.setenv("SCREENSCRIBE_API_KEY", "sk-env-only-generic")
    _signed_in_openai(monkeypatch)

    result = CliRunner().invoke(app, ["config", "auth", "chatgpt"], input="y\n")

    assert result.exit_code == 0, result.output
    text = config_env.read_text(encoding="utf-8")
    assert "SCREENSCRIBE_PROVIDER=chatgpt" in text
    for secret in ("test-libraxis-env-only", "xai-env-only-tts", "sk-env-only-generic"):
        assert secret not in text
    # The environment still supplies the STT key at run time.
    assert ScreenScribeConfig.load().get_stt_api_key() == "test-libraxis-env-only"


def test_chatgpt_switch_keeps_vision_off_and_the_egress_policy() -> None:
    source = ScreenScribeConfig(use_vision_analysis=False, agent_egress="allow")
    source.agent_primary_trust = "internal"
    preset = ScreenScribeConfig.provider_preset("chatgpt", "", stt_from=source)
    assert preset.use_vision_analysis is False
    assert preset.agent_egress == "allow"
    # Host-specific trust never follows the selection to chatgpt.com.
    assert preset.agent_primary_trust == ""


def test_bearer_resolution_repr_never_shows_the_token() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-secret-xai"))
    resolution = resolve_bearer("xai", "", host="api.x.ai")
    assert resolution.usable and resolution.bearer == "at-secret-xai"
    assert "at-secret-xai" not in repr(resolution)
    assert "sk-explicit-secret" not in repr(resolve_bearer("xai", "sk-explicit-secret"))
