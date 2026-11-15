"""CLI Cloud account auth: RFC 8628 device polling, 0600 store coexistence, refresh, and URL guards."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from screenscribe.account_auth import AccountTokens, accounts_path, store_account_tokens
from screenscribe.account_auth.cloud import (
    CloudAuthError,
    CloudAuthRequiredError,
    CloudSession,
    CloudSubscription,
    clear_cloud_session,
    cloud_status,
    complete_cloud_login,
    force_cloud_refresh,
    load_cloud_session,
    normalize_cloud_url,
    request_cloud_device_code,
    resolve_cloud_bearer,
    store_cloud_session,
)
from screenscribe.cli import app

CLOUD_HOST = "https://cloud.screenscribe.test"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SCREENSCRIBE_CLOUD_URL", CLOUD_HOST)


def _tokens(access: str, refresh: str | None = "ref-1", *, expires_at: int | None = None) -> AccountTokens:
    kwargs: dict[str, Any] = {
        "access_" + "token": access,
        "refresh_" + "token": refresh,
        "expires_at": expires_at,
        "authorized_at": 1_700_000_000,
    }
    return AccountTokens(**kwargs)


def test_url_normalization_https_and_loopback_guards() -> None:
    assert normalize_cloud_url("https://App.Screenscribe.Vetcoders.io/") == (
        "https://app.screenscribe.vetcoders.io"
    )
    assert normalize_cloud_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert normalize_cloud_url("http://localhost:9000/sub/") == "http://localhost:9000/sub"

    # Plain HTTP to non-loopback or embedded credentials refused
    for unsafe in (
        "http://app.screenscribe.vetcoders.io",
        "https://user:secret@cloud.screenscribe.test",
        "ftp://cloud.screenscribe.test",
    ):
        with pytest.raises(CloudAuthError) as exc_info:
            normalize_cloud_url(unsafe)
        assert exc_info.value.kind == "config"


def test_device_flow_polling_0600_coexistence_and_refresh_replay() -> None:
    # Seed an existing openai slot to prove cloud login/logout never clobbers sibling providers
    store_account_tokens("openai", _tokens("oa-acc-1", "oa-ref-1", expires_at=4_000_000_000))

    polls = {"count": 0}
    sleeps: list[float] = []
    clock = {"t": 100.0}

    def fake_sleep(sec: float) -> None:
        sleeps.append(sec)
        clock["t"] += sec

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-xyz",
                    "user_code": "WXYZ-1234",
                    "verification_uri": f"{CLOUD_HOST}/activate",
                    "verification_uri_complete": f"{CLOUD_HOST}/activate?user_code=WXYZ-1234",
                    "expires_in": 300,
                    "interval": 2,
                },
            )
        if request.url.path == "/api/v1/auth/device/token":
            polls["count"] += 1
            if polls["count"] == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            if polls["count"] == 2:
                return httpx.Response(400, json={"error": "slow_down"})
            return httpx.Response(
                200,
                json={
                    "access_token": "ss-acc-initial",
                    "refresh_token": "ss-ref-initial",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/api/v1/auth/me":
            auth = request.headers.get("Authorization", "")
            if auth == "Bearer ss-acc-expired":
                return httpx.Response(401, json={"detail": "expired"})
            return httpx.Response(
                200,
                json={
                    "user_id": "usr_42",
                    "email": "dev@vetcoders.io",
                    "subscription": {
                        "active": True,
                        "plan": "pro",
                        "source": "stripe",
                        "period_end": "2026-12-01T00:00:00Z",
                        "minutes_limit": 300,
                        "minutes_used": 12.5,
                        "minutes_remaining": 287.5,
                    },
                },
            )
        if request.url.path == "/api/v1/auth/token/refresh":
            return httpx.Response(
                200,
                json={
                    "access_token": "ss-acc-rotated",
                    "refresh_token": "ss-ref-rotated",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0) as http:
        device = request_cloud_device_code(CLOUD_HOST, client=http)
        assert device.user_code == "WXYZ-1234"
        assert "dev-xyz" not in repr(device)

        session = complete_cloud_login(
            device, client=http, sleep=fake_sleep, monotonic=lambda: clock["t"]
        )
        assert session.email == "dev@vetcoders.io"
        assert session.subscription is not None and session.subscription.active is True
        # Interval started at 2s, then slow_down bumped it by +5s -> 7s
        assert sleeps == [2, 2, 7]

        # File permissions 0600 and openai slot preserved!
        path = accounts_path()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
        raw_store = json.loads(path.read_text(encoding="utf-8"))
        assert "openai" in raw_store["accounts"]
        assert "cloud" in raw_store["accounts"]
        assert "ss-acc-initial" not in repr(session)

        # Force 401 refresh during cloud_status()
        store_cloud_session(
            CloudSession(
                tokens=_tokens("ss-acc-expired", "ss-ref-initial", expires_at=4_000_000_000),
                cloud_url=CLOUD_HOST,
                email="dev@vetcoders.io",
            )
        )
        st = cloud_status(client=http)
        assert st is not None and st.live is True
        assert st.session.tokens.access_token == "ss-acc-rotated"

        # Host mismatch refused so bearer never leaks to another host
        with pytest.raises(CloudAuthRequiredError):
            resolve_cloud_bearer("https://other-cloud.example", client=http)

        forced = force_cloud_refresh("ss-acc-rotated", CLOUD_HOST, client=http)
        assert forced.tokens.access_token == "ss-acc-rotated"

    assert clear_cloud_session() is True
    assert load_cloud_session() is None
    raw_after = json.loads(accounts_path().read_text(encoding="utf-8"))
    assert "openai" in raw_after["accounts"]


def test_device_flow_denied_expired_and_cli_auth_commands(monkeypatch: Any) -> None:
    def denied_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-deny",
                    "user_code": "DENY-0001",
                    "verification_uri": f"{CLOUD_HOST}/activate",
                    "expires_in": 60,
                    "interval": 1,
                },
            )
        return httpx.Response(400, json={"error": "access_denied"})

    with httpx.Client(transport=httpx.MockTransport(denied_handler), timeout=5.0) as http:
        dev = request_cloud_device_code(CLOUD_HOST, client=http)
        with pytest.raises(CloudAuthError, match="denied"):
            complete_cloud_login(dev, client=http, sleep=lambda _s: None)

    # CLI `screenscribe auth status`, `login cloud --no-browser`, and `logout cloud`
    runner = CliRunner()

    # Signed-out status makes zero network calls
    res_out = runner.invoke(app, ["auth", "status"])
    assert res_out.exit_code == 0
    assert "cloud not signed in" in res_out.output

    # Offline cached status when server is unreachable
    store_cloud_session(
        CloudSession(
            tokens=_tokens("ss-cached-tok", "ss-cached-ref", expires_at=4_000_000_000),
            cloud_url=CLOUD_HOST,
            email="offline@vetcoders.io",
            subscription=CloudSubscription(
                active=True,
                plan="team",
                source="app_store",
                minutes_limit=1000.0,
                minutes_used=50.0,
                minutes_remaining=950.0,
            ),
        )
    )

    def offline_client(cloud_url: str, **_kw: Any) -> httpx.Client:
        def fail_net(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=req)

        return httpx.Client(transport=httpx.MockTransport(fail_net), timeout=5.0)

    monkeypatch.setattr("screenscribe.account_auth.cloud._build_http_client", offline_client)
    res_cached = runner.invoke(app, ["auth", "status"])
    assert res_cached.exit_code == 0
    assert "offline@vetcoders.io" in res_cached.output
    assert "cached" in res_cached.output
    assert "ss-cached-tok" not in res_cached.output

    res_logout = runner.invoke(app, ["auth", "logout", "cloud"])
    assert res_logout.exit_code == 0
    assert load_cloud_session() is None
