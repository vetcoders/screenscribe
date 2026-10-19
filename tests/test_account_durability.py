"""Account-store durability: cross-process lock, peer rotation, failure classes.

Pins the refresh design ported from oh-my-pi ``auth/refresh.ts``: one exclusive
``flock`` around every read-modify-write of ``accounts.json``, a peer's fresh
rotation reused instead of spending a single-use refresh token twice, and a
definitive provider refusal that marks the account as needing a new sign-in.

Every HTTP call goes through ``httpx.MockTransport`` -- no network. ``Path.home``
(and ``HOME`` for spawned children) is redirected to ``tmp_path`` so the real
``~/.config/screenscribe/accounts.json`` is never touched.
"""

# ruff: noqa: S105, S106 -- fixture tokens are literal test strings, not credentials
from __future__ import annotations

import base64
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from screenscribe import account_auth
from screenscribe.account_auth import (
    AccountAuthError,
    AccountTokens,
    account_status,
    accounts_path,
    force_refresh,
    load_account_tokens,
    refresh_tokens,
    resolve_bearer,
    store_account_tokens,
)
from screenscribe.account_auth.device_code import (
    complete_device_code_login,
    request_device_code,
)
from screenscribe.config import ScreenScribeConfig

OPENAI_ISSUER = "https://auth.openai.test"
XAI_ISSUER = "https://auth.xai.test"
POSIX = os.name == "posix" and sys.platform != "win32"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Spawned children do not inherit the monkeypatch; HOME keeps them in tmp_path too.
    monkeypatch.setenv("HOME", str(tmp_path))
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


def _token_path(provider: str) -> str:
    return account_auth.provider_oauth_config(provider).exchange_path


def _lock_path() -> Path:
    return accounts_path().parent / account_auth.ACCOUNTS_LOCK_NAME


def _stored_entry(provider: str) -> dict[str, Any]:
    return json.loads(accounts_path().read_text(encoding="utf-8"))["accounts"][provider]


def _response(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status) if body is None else httpx.Response(status, json=body)


# --------------------------------------------------------------------------- #
# 1. Store lock: 0600 lock file, bounded wait on a lock held by another process
# --------------------------------------------------------------------------- #

_HOLD_LOCK_SCRIPT = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "sys.stdin.read()\n"  # hold the lock until the parent closes stdin
)


@pytest.mark.skipif(not POSIX, reason="flock store lock is POSIX-only")
def test_store_lock_file_is_private() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-xai"))
    lock = _lock_path()
    assert lock.exists()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(accounts_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(not POSIX, reason="flock store lock is POSIX-only")
def test_lock_held_by_another_process_times_out_as_storage(monkeypatch: Any) -> None:
    lock = _lock_path()
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", _HOLD_LOCK_SCRIPT, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdin is not None
        assert holder.stdout.readline().strip() == "locked"

        started = time.monotonic()
        with pytest.raises(AccountAuthError) as direct:
            with account_auth._store_lock(timeout=0.2):
                pytest.fail("acquired a lock another process holds")
        waited = time.monotonic() - started
        assert direct.value.kind == "storage"
        assert "busy" in str(direct.value)
        assert 0.2 <= waited < 5.0

        # The default timeout is bound at import, so shorten it through the
        # module-level lock that store_account_tokens resolves at call time.
        original = account_auth._store_lock
        monkeypatch.setattr(
            account_auth, "_store_lock", lambda timeout=0.2: original(timeout=timeout)
        )
        with pytest.raises(AccountAuthError) as stored:
            store_account_tokens("xai", AccountTokens(access_token="at-blocked"))
        assert stored.value.kind == "storage"
        assert not accounts_path().exists()  # nothing written while another process holds it
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=10)
    assert holder.returncode == 0

    # Released lock: the same write goes through.
    store_account_tokens("xai", AccountTokens(access_token="at-after"))
    assert load_account_tokens("xai").access_token == "at-after"


# --------------------------------------------------------------------------- #
# 2. Lost-update fix: concurrent writers of different slots both survive
# --------------------------------------------------------------------------- #

#: Without the store lock, 120 interleaved writes per process lose an update in
#: most runs (probed: 4/5); with it, never.
ROUNDS = 120


def _store_worker(home: str, provider: str, rounds: int, barrier: Any) -> None:
    """Spawned child: write ``provider``'s slot ``rounds`` times inside ``home``."""
    Path.home = lambda: Path(home)  # type: ignore[method-assign,assignment]
    from screenscribe.account_auth import AccountTokens, accounts_path, store_account_tokens

    if Path(home) not in accounts_path().parents:
        raise SystemExit(f"refusing to write outside the test home: {accounts_path()}")
    barrier.wait(timeout=30)
    for index in range(rounds):
        store_account_tokens(provider, AccountTokens(access_token=f"{provider}-{index}"))


@pytest.mark.skipif(not POSIX, reason="flock store lock is POSIX-only")
def test_concurrent_writers_of_different_providers_keep_both_slots(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    workers = [
        ctx.Process(target=_store_worker, args=(str(tmp_path), provider, ROUNDS, barrier))
        for provider in ("xai", "openai")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    for worker in workers:
        if worker.is_alive():
            worker.kill()
        assert worker.exitcode == 0

    accounts = json.loads(accounts_path().read_text(encoding="utf-8"))["accounts"]
    assert set(accounts) == {"xai", "openai"}
    assert accounts["xai"]["access_token"] == f"xai-{ROUNDS - 1}"
    assert accounts["openai"]["access_token"] == f"openai-{ROUNDS - 1}"
    leftovers = [p.name for p in accounts_path().parent.iterdir() if p.name.startswith(".accounts")]
    assert leftovers == []  # every atomic temp file was renamed or removed


# --------------------------------------------------------------------------- #
# 3. Peer rotation: reuse a fresh stored token; spend only the newest refresh token
# --------------------------------------------------------------------------- #


def test_refresh_reuses_peer_rotation_without_http() -> None:
    peer = AccountTokens(
        access_token="at-peer", refresh_token="rt-peer", expires_at=int(time.time()) + 3600
    )
    store_account_tokens("xai", peer)
    before = accounts_path().read_bytes()
    stale = AccountTokens(access_token="at-caller", refresh_token="rt-caller", expires_at=1)
    rec = Recorder()
    with rec.client() as client:
        result = refresh_tokens("xai", stale, client=client)
    assert rec.calls == []
    assert result.access_token == "at-peer"
    assert result.refresh_token == "rt-peer"
    assert accounts_path().read_bytes() == before


@pytest.mark.parametrize(
    ("rotated", "expected_refresh"),
    [("rt-next", "rt-next"), (None, "rt-peer-newest")],
    ids=["provider-rotates", "provider-keeps"],
)
def test_refresh_of_expiring_peer_token_spends_the_stored_refresh_token(
    rotated: str | None, expected_refresh: str
) -> None:
    store_account_tokens(
        "xai",
        AccountTokens(
            access_token="at-peer",
            refresh_token="rt-peer-newest",
            expires_at=int(time.time()) + 10,  # inside the 60 s skew
        ),
    )
    body: dict[str, Any] = {"access_token": "at-new", "expires_in": 3600}
    if rotated:
        body["refresh_token"] = rotated
    rec = Recorder()
    rec.script(_token_path("xai"), httpx.Response(200, json=body))
    stale = AccountTokens(access_token="at-caller", refresh_token="rt-caller-spent", expires_at=1)
    with rec.client() as client:
        result = refresh_tokens("xai", stale, client=client)
    assert len(rec.calls) == 1
    assert _form(rec.calls[0])["refresh_token"] == "rt-peer-newest"
    assert result.access_token == "at-new"
    assert result.refresh_token == expected_refresh
    stored = load_account_tokens("xai")
    assert stored.access_token == "at-new"
    assert stored.refresh_token == expected_refresh


# --------------------------------------------------------------------------- #
# 4. force_refresh after an API 401
# --------------------------------------------------------------------------- #


def test_force_refresh_returns_peer_replacement_even_when_expiring() -> None:
    store_account_tokens(
        "xai", AccountTokens(access_token="at-peer", refresh_token="rt-peer", expires_at=1)
    )
    rec = Recorder()
    with rec.client() as client:
        result = force_refresh("xai", "at-rejected", client=client)
    assert rec.calls == []
    assert result.access_token == "at-peer"


def test_force_refresh_of_the_rejected_token_sends_one_request() -> None:
    store_account_tokens(
        "xai",
        AccountTokens(
            access_token="at-rejected",
            refresh_token="rt-live",
            expires_at=int(time.time()) + 3600,  # not expiring: forced anyway
        ),
    )
    rec = Recorder()
    rec.script(
        _token_path("xai"),
        httpx.Response(200, json={"access_token": "at-forced", "expires_in": 3600}),
    )
    with rec.client() as client:
        result = force_refresh("xai", "at-rejected", client=client)
    assert rec.paths() == [_token_path("xai")]
    assert _form(rec.calls[0])["refresh_token"] == "rt-live"
    assert result.access_token == "at-forced"
    assert load_account_tokens("xai").access_token == "at-forced"


def test_force_refresh_without_an_account_is_not_signed_in() -> None:
    rec = Recorder()
    with rec.client() as client, pytest.raises(AccountAuthError) as excinfo:
        force_refresh("xai", "at-rejected", client=client)
    assert excinfo.value.kind == "not_signed_in"
    assert rec.calls == []


# --------------------------------------------------------------------------- #
# 5. Refresh failure classification
# --------------------------------------------------------------------------- #

_DEFINITIVE = [
    pytest.param("xai", 400, {"error": "invalid_grant"}, id="400-invalid_grant"),
    pytest.param("xai", 401, {"error": "invalid_token"}, id="401-invalid_token"),
    pytest.param(
        "xai",
        400,
        {"error": "invalid_request", "error_description": "Refresh token was Revoked by user"},
        id="400-description-revoked",
    ),
    pytest.param("xai", 401, None, id="401-bare"),
    pytest.param("xai", 403, {"error": "access_denied"}, id="403-access_denied"),
    pytest.param(
        "openai",
        401,
        {
            "error": {
                "code": "refresh_token_expired",
                "message": "Your refresh token has expired. Please sign in again.",
            }
        },
        id="openai-nested-expired-message",
    ),
]

_TRANSIENT = [
    pytest.param(429, {"error": "rate_limited"}, id="429"),
    pytest.param(500, None, id="500-bare"),
    pytest.param(502, {"error": "bad_gateway"}, id="502"),
    pytest.param(503, {"error": "invalid_grant"}, id="503-even-with-invalid_grant"),
]


@pytest.mark.parametrize(("provider", "status", "body"), _DEFINITIVE)
def test_classifier_marks_definitive_refusals_revoked(
    provider: str, status: int, body: Any
) -> None:
    assert account_auth._classify_refresh_failure(_response(status, body)).kind == "revoked"


@pytest.mark.parametrize(("status", "body"), _TRANSIENT)
def test_classifier_keeps_temporary_failures_retryable(status: int, body: Any) -> None:
    assert account_auth._classify_refresh_failure(_response(status, body)).kind == "http"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(400, {"error": "invalid_request"}, id="400-invalid_request"),
        pytest.param(400, None, id="400-bare"),
        pytest.param(404, {"error": "invalid_grant"}, id="404-not-an-oauth-refusal"),
    ],
)
def test_classifier_other_refusals_stay_oauth(status: int, body: Any) -> None:
    assert account_auth._classify_refresh_failure(_response(status, body)).kind == "oauth"


@pytest.mark.parametrize(("provider", "status", "body"), _DEFINITIVE)
def test_definitive_refusal_disables_the_stored_account(
    provider: str, status: int, body: Any
) -> None:
    host = account_auth.provider_oauth_config(provider).bearer_hosts[0]
    tokens = AccountTokens(access_token="at-dead", refresh_token="rt-dead", expires_at=1)
    store_account_tokens(provider, tokens)
    rec = Recorder()
    rec.script(_token_path(provider), _response(status, body))
    with rec.client() as client:
        with pytest.raises(AccountAuthError) as excinfo:
            refresh_tokens(provider, tokens, client=client)
        assert excinfo.value.kind == "revoked"
        assert len(rec.calls) == 1

        status_row = account_status(provider)
        assert status_row.signed_in is True and status_row.needs_login is True
        assert _stored_entry(provider)["disabled_cause"]

        resolution = resolve_bearer(provider, "", host=host, client=client)
        assert resolution.bearer == "" and resolution.source == "none"
        assert resolution.usable is False
        assert resolution.warning and f"screenscribe auth login {provider}" in resolution.warning
        assert "at-dead" not in resolution.warning
        assert len(rec.calls) == 1  # a disabled grant is never refreshed again

        with pytest.raises(AccountAuthError) as again:
            force_refresh(provider, "at-dead", client=client)
        assert again.value.kind == "revoked"
        assert len(rec.calls) == 1

    # A new sign-in replaces the disabled entry.
    store_account_tokens(
        provider,
        AccountTokens(access_token="at-fresh", refresh_token="rt-fresh", authorized_at=123),
    )
    assert account_status(provider).needs_login is False
    fresh = resolve_bearer(provider, "", host=host)
    assert fresh.usable and fresh.bearer == "at-fresh"


@pytest.mark.parametrize(("status", "body"), _TRANSIENT)
def test_temporary_refresh_failure_keeps_the_account(status: int, body: Any) -> None:
    tokens = AccountTokens(access_token="at-old", refresh_token="rt-old", expires_at=1)
    store_account_tokens("xai", tokens)
    rec = Recorder()
    rec.script(_token_path("xai"), _response(status, body))
    with rec.client() as client, pytest.raises(AccountAuthError) as excinfo:
        refresh_tokens("xai", tokens, client=client)
    assert excinfo.value.kind == "http"
    assert account_status("xai").needs_login is False
    assert (
        "disabled_cause" not in _stored_entry("xai") or not _stored_entry("xai")["disabled_cause"]
    )
    assert load_account_tokens("xai").refresh_token == "rt-old"


def test_transport_error_is_retryable_and_keeps_the_account() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    tokens = AccountTokens(access_token="at-old", refresh_token="rt-old", expires_at=1)
    store_account_tokens("xai", tokens)
    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(AccountAuthError) as excinfo:
            refresh_tokens("xai", tokens, client=client)
    assert excinfo.value.kind == "http"
    assert account_status("xai").needs_login is False


def test_non_definitive_refusal_is_oauth_and_keeps_the_account() -> None:
    tokens = AccountTokens(access_token="at-old", refresh_token="rt-old", expires_at=1)
    store_account_tokens("xai", tokens)
    rec = Recorder()
    rec.script(_token_path("xai"), httpx.Response(400, json={"error": "invalid_request"}))
    with rec.client() as client, pytest.raises(AccountAuthError) as excinfo:
        refresh_tokens("xai", tokens, client=client)
    assert excinfo.value.kind == "oauth"
    assert account_status("xai").needs_login is False
    assert load_account_tokens("xai").access_token == "at-old"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "error": {
                    "code": "refresh_token_reused",
                    "message": "Your refresh token has already been used to generate a new "
                    "access token. Please try signing in again.",
                }
            },
            id="reused",
        ),
        pytest.param(
            {
                "error": {
                    "code": "refresh_token_invalidated",
                    "message": "Please try signing in again.",
                }
            },
            id="invalidated",
        ),
        pytest.param(
            {"error": {"code": "refresh_token_expired", "message": "Please sign in again."}},
            id="expired-code-only",
        ),
    ],
)
def test_openai_nested_dead_grant_codes_are_definitive(body: dict[str, Any]) -> None:
    assert account_auth._classify_refresh_failure(_response(401, body)).kind == "revoked"


# --------------------------------------------------------------------------- #
# 6. resolve_bearer keeps a still-valid token through a temporary failure
# --------------------------------------------------------------------------- #


def test_resolve_bearer_inside_skew_survives_temporary_failure() -> None:
    expires_at = int(time.time()) + 30  # inside the 60 s skew, not expired
    store_account_tokens(
        "xai", AccountTokens(access_token="at-live", refresh_token="rt", expires_at=expires_at)
    )
    rec = Recorder()
    rec.script(_token_path("xai"), httpx.Response(500))
    with rec.client() as client:
        resolution = resolve_bearer("xai", "", host="api.x.ai", client=client)
    assert rec.paths() == [_token_path("xai")]
    assert resolution.usable and resolution.bearer == "at-live"
    assert resolution.source == "account"
    assert resolution.warning and "refresh failed (http)" in resolution.warning
    assert "at-live" not in resolution.warning
    assert account_status("xai").needs_login is False


def test_resolve_bearer_expired_token_with_temporary_failure_is_none() -> None:
    store_account_tokens(
        "xai",
        AccountTokens(access_token="at-dead", refresh_token="rt", expires_at=int(time.time()) - 5),
    )
    rec = Recorder()
    rec.script(_token_path("xai"), httpx.Response(503))
    with rec.client() as client:
        resolution = resolve_bearer("xai", "", host="api.x.ai", client=client)
    assert resolution.bearer == "" and resolution.source == "none"
    assert resolution.warning and "screenscribe auth login xai" in resolution.warning
    assert account_status("xai").needs_login is False  # retryable: grant kept


def test_resolve_bearer_definitive_failure_drops_a_not_yet_expired_token() -> None:
    store_account_tokens(
        "xai",
        AccountTokens(access_token="at-live", refresh_token="rt", expires_at=int(time.time()) + 30),
    )
    rec = Recorder()
    rec.script(_token_path("xai"), httpx.Response(400, json={"error": "invalid_grant"}))
    with rec.client() as client:
        resolution = resolve_bearer("xai", "", host="api.x.ai", client=client)
    assert resolution.bearer == "" and resolution.source == "none"
    assert resolution.warning and "screenscribe auth login xai" in resolution.warning
    assert account_status("xai").needs_login is True


# --------------------------------------------------------------------------- #
# 7. authorized_at: stamped at sign-in, preserved across refreshes
# --------------------------------------------------------------------------- #


def test_authorized_at_is_stamped_at_sign_in_and_survives_refresh(monkeypatch: Any) -> None:
    rec = Recorder()
    rec.script(
        "/oauth2/device/code",
        httpx.Response(
            200,
            json={
                "device_code": "dc-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://auth.x.ai/device",
                "interval": 5,
                "expires_in": 600,
            },
        ),
    )
    rec.script(
        "/oauth2/token",
        httpx.Response(
            200,
            json={
                "access_token": "at-login",
                "refresh_token": "rt-login",
                "expires_in": 3600,
            },
        ),
        httpx.Response(
            200,
            json={"access_token": "at-refreshed", "refresh_token": "rt-2", "expires_in": 3600},
        ),
    )
    before = int(time.time())
    with rec.client() as client:
        device = request_device_code("xai", client=client)
        tokens = complete_device_code_login(device, client=client, sleep=rec.sleep)
        after = int(time.time())

        assert tokens.authorized_at is not None
        signed_in_at = tokens.authorized_at
        assert before <= signed_in_at <= after
        assert _stored_entry("xai")["authorized_at"] == signed_in_at
        assert account_status("xai").authorized_at == signed_in_at

        # Move the clock so a re-stamp would be visible, then refresh.
        later = float(signed_in_at + 1000)
        monkeypatch.setattr(time, "time", lambda: later)
        refreshed = force_refresh("xai", "at-login", client=client)

    assert refreshed.access_token == "at-refreshed"
    assert refreshed.expires_at == int(later) + 3600  # the clock really moved
    assert refreshed.authorized_at == signed_in_at
    assert _stored_entry("xai")["authorized_at"] == signed_in_at
    assert account_status("xai").authorized_at == signed_in_at


# --------------------------------------------------------------------------- #
# 8. Host-aware bearer usability
# --------------------------------------------------------------------------- #


def test_openai_account_token_is_usable_only_on_chatgpt() -> None:
    store_account_tokens("openai", AccountTokens(access_token="chatgpt-token", expires_at=None))

    chatgpt = resolve_bearer("openai", "", host="chatgpt.com")
    assert chatgpt.usable and chatgpt.identity_only is False
    assert chatgpt.bearer == "chatgpt-token" and chatgpt.warning is None

    api = resolve_bearer("openai", "", host="api.openai.com")
    assert api.identity_only is True and api.usable is False
    assert api.warning and "2026-09-08" in api.warning

    unspecified = resolve_bearer("openai", "")
    assert unspecified.identity_only is True and unspecified.usable is False


def test_xai_account_token_is_usable_on_api_x_ai_only() -> None:
    store_account_tokens("xai", AccountTokens(access_token="at-xai", expires_at=None))
    assert resolve_bearer("xai", "", host="api.x.ai").usable
    assert resolve_bearer("xai", "").usable  # xAI has no identity-only host
    foreign = resolve_bearer("xai", "", host="other.example")
    assert foreign.usable is False and foreign.identity_only is True


# --------------------------------------------------------------------------- #
# 9. Unsafe issuer is refused before the store, the lock, or the network
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("call", ["refresh_tokens", "force_refresh"])
def test_unsafe_issuer_is_refused_before_store_lock_or_http(call: str, monkeypatch: Any) -> None:
    monkeypatch.setenv("SCREENSCRIBE_XAI_OAUTH_ISSUER", "http://example.invalid")
    path = accounts_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")  # reading it would be a "storage" error
    before = path.read_bytes()
    rec = Recorder()
    with rec.client() as client, pytest.raises(AccountAuthError) as excinfo:
        if call == "refresh_tokens":
            refresh_tokens(
                "xai",
                AccountTokens(access_token="at", refresh_token="rt", expires_at=1),
                client=client,
            )
        else:
            force_refresh("xai", "at", client=client)
    assert excinfo.value.kind == "http"
    assert "HTTPS" in str(excinfo.value)
    assert "example.invalid" not in str(excinfo.value)
    assert rec.calls == []
    assert path.read_bytes() == before
    assert not _lock_path().exists()
