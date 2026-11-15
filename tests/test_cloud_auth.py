"""Cloud authentication, session CSRF, Apple JWS, RFC 8628 device flow, and account deletion tests."""

from __future__ import annotations

import io
import json
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from screenscribe.cloud.app import create_cloud_app
from screenscribe.cloud.jws import (
    APPLE_ISSUER,
    AppleJwksVerifier,
    JwsError,
    StaticJwksVerifier,
    validate_identity_claims,
)
from screenscribe.cloud.security import CSRF_COOKIE, CSRF_FORM_FIELD, CSRF_HEADER, SESSION_COOKIE
from tests.cloud_test_helpers import (
    auth_payload,
    ec_jwk,
    make_cloud_settings,
    make_ec_keypair,
    make_rsa_keypair,
    rsa_jwk,
    sign_es256_jws,
    sign_rs256_jws,
)


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def test_register_login_me_and_token_rotation_reuse_revocation(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC))
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings, clock=clock)

    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json=auth_payload("Alice@Example.com", "correct-horse-battery", display_name="Alice"),
        )
        assert reg.status_code == 201
        first_bundle = reg.json()
        assert first_bundle["user"]["email"] == "alice@example.com"
        bearer_1 = first_bundle["access_token"]
        refresh_1 = first_bundle["refresh_token"]

        # Duplicate email rejected
        dup = client.post(
            "/api/v1/auth/register",
            json=auth_payload("alice@example.com", "another-valid-phrase"),
        )
        assert dup.status_code == 409

        # Wrong credentials rejected with 401
        bad = client.post(
            "/api/v1/auth/login",
            json=auth_payload("alice@example.com", "wrong-phrase-value"),
        )
        assert bad.status_code == 401
        missing = client.post(
            "/api/v1/auth/login",
            json=auth_payload("nobody@example.com", "wrong-phrase-value"),
        )
        assert missing.status_code == 401

        # /me returns identity + inactive subscription snapshot
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bearer_1}"})
        assert me.status_code == 200
        assert me.json()["email"] == "alice@example.com"
        assert me.json()["subscription"]["active"] is False

        # Rotate via /api/v1/auth/token/refresh AND /api/v1/auth/refresh alias
        rot1 = client.post(
            "/api/v1/auth/token/refresh",
            json={"grant_type": "refresh_token", "refresh_token": refresh_1},
        )
        assert rot1.status_code == 200
        bearer_2 = rot1.json()["access_token"]
        refresh_2 = rot1.json()["refresh_token"]
        assert bearer_2 != bearer_1
        assert refresh_2 != refresh_1

        rot2 = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_2},
        )
        assert rot2.status_code == 200
        bearer_3 = rot2.json()["access_token"]

        # Presenting already-rotated refresh_1 triggers family revocation!
        reuse = client.post(
            "/api/v1/auth/token/refresh",
            json={"grant_type": "refresh_token", "refresh_token": refresh_1},
        )
        assert reuse.status_code == 400
        assert reuse.json()["error"] == "invalid_grant"

        # Now bearer_3 in that token family is also revoked
        after_reuse = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {bearer_3}"}
        )
        assert after_reuse.status_code == 401


def test_cookie_session_csrf_and_origin_enforcement(tmp_path: Path) -> None:
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings)

    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json=auth_payload("web@example.com", "web-portal-phrase", cookie=True),
        )
        assert reg.status_code == 201
        assert SESSION_COOKIE in client.cookies
        csrf_val = client.cookies.get(CSRF_COOKIE)
        assert csrf_val

        # Cookie GET succeeds without CSRF header
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200

        # Cookie state-changing POST without Origin / CSRF fails with 403
        no_origin = client.post("/api/v1/auth/logout")
        assert no_origin.status_code == 403

        cross_site = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://evil.example", CSRF_HEADER: csrf_val},
        )
        assert cross_site.status_code == 403

        missing_csrf = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://testserver"},
        )
        assert missing_csrf.status_code == 403

        # Valid Origin + X-CSRF-Token succeeds and revokes cookie session
        ok_logout = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://testserver", CSRF_HEADER: csrf_val},
        )
        assert ok_logout.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401


def test_apple_sign_in_rs256_es256_and_jwks_rotation(tmp_path: Path) -> None:
    rsa_pub, rsa_d = make_rsa_keypair()
    ec_pub, ec_d = make_ec_keypair("P-256", 43)

    jwks_calls = {"count": 0}

    def jwks_handler(request: httpx.Request) -> httpx.Response:
        jwks_calls["count"] += 1
        keys = [rsa_jwk(rsa_pub, kid="apple-rsa-1")]
        if jwks_calls["count"] >= 2:
            keys.append(ec_jwk(ec_pub, kid="apple-ec-2"))
        return httpx.Response(200, json={"keys": keys})

    transport = httpx.MockTransport(jwks_handler)
    verifier = AppleJwksVerifier(
        "https://appleid.apple.com/auth/keys", cache_seconds=3600.0, transport=transport
    )
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings, apple_verifier=verifier, http_transport=transport)

    now = int(time.time())
    rs_claims = {
        "iss": APPLE_ISSUER,
        "aud": "io.vetcoders.screenscribe.mac",
        "sub": "apple-user-001",
        "email": "macuser@example.com",
        "email_verified": "true",
        "iat": now - 10,
        "exp": now + 600,
    }
    rs_jwt = sign_rs256_jws({"alg": "RS256", "kid": "apple-rsa-1"}, rs_claims, rsa_pub, rsa_d)

    es_claims = {
        "iss": APPLE_ISSUER,
        "aud": "io.vetcoders.screenscribe.web",
        "sub": "apple-user-002",
        "iat": now - 5,
        "exp": now + 600,
    }
    es_jwt = sign_es256_jws({"alg": "ES256", "kid": "apple-ec-2"}, es_claims, ec_d)

    bad_aud_jwt = sign_es256_jws(
        {"alg": "ES256", "kid": "apple-ec-2"},
        {**es_claims, "aud": "com.other.bundle"},
        ec_d,
    )

    with TestClient(app) as client:
        res1 = client.post(
            "/api/v1/auth/apple",
            json={"identity_token": rs_jwt, "display_name": "Mac User"},
        )
        assert res1.status_code == 200
        assert res1.json()["user"]["email"] == "macuser@example.com"
        assert res1.json()["user"]["apple_linked"] is True
        assert jwks_calls["count"] == 1

        # Second sign-in with rotated kid triggers single JWKS refetch and succeeds
        res2 = client.post(
            "/api/v1/auth/apple",
            json={
                "identity_token": es_jwt,
                "email": "private-relay@example.com",
                "display_name": "Relay User",
            },
        )
        assert res2.status_code == 200
        assert res2.json()["user"]["email"] == "private-relay@example.com"
        assert jwks_calls["count"] == 2

        # Wrong audience rejected with 401
        bad = client.post("/api/v1/auth/apple", json={"identity_token": bad_aud_jwt})
        assert bad.status_code == 401

    # Also exercise StaticJwksVerifier and claim edge cases directly
    static_v = StaticJwksVerifier({"keys": [ec_jwk(ec_pub, kid="apple-ec-2")]})
    with pytest.raises(JwsError):
        validate_identity_claims(
            {"iss": "https://evil.example", "aud": "a", "sub": "s", "exp": now + 60},
            audiences=("a",),
            now=float(now),
        )
    assert static_v is not None


def test_rfc8628_device_flow_lifecycle(tmp_path: Path) -> None:
    clock = _MutableClock(datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC))
    settings = make_cloud_settings(
        tmp_path / "storage", device_poll_interval=5, device_code_ttl_seconds=60
    )
    app = create_cloud_app(settings, clock=clock)

    with TestClient(app) as client:
        # Start device authorization
        start = client.post("/api/v1/auth/device/code", json={"client_id": "screenscribe-cli"})
        assert start.status_code == 200
        code_body = start.json()
        dev_code = code_body["device_code"]
        user_code = code_body["user_code"]
        assert code_body["verification_uri"].endswith("/activate")

        # 1st poll -> authorization_pending
        poll_1 = client.post(
            "/api/v1/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dev_code,
            },
        )
        assert poll_1.status_code == 400
        assert poll_1.json()["error"] == "authorization_pending"

        # Immediate 2nd poll (<5s) -> slow_down
        clock.advance(1.0)
        poll_fast = client.post(
            "/api/v1/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dev_code,
            },
        )
        assert poll_fast.status_code == 400
        assert poll_fast.json()["error"] == "slow_down"

        # Sign in a web user and approve on /activate
        reg = client.post(
            "/api/v1/auth/register",
            json=auth_payload("cli-owner@example.com", "cli-owner-phrase", cookie=True),
        )
        assert reg.status_code == 201
        csrf_val = client.cookies.get(CSRF_COOKIE) or ""

        approve = client.post(
            "/activate",
            data={
                CSRF_FORM_FIELD: csrf_val,
                "user_code": user_code,
                "decision": "approve",
            },
            headers={"Origin": "http://testserver"},
        )
        assert approve.status_code == 200

        clock.advance(12.0)
        poll_ok = client.post(
            "/api/v1/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dev_code,
            },
        )
        assert poll_ok.status_code == 200
        issued = poll_ok.json()
        assert issued["token_type"] == "Bearer"

        # Second consumption of the same device_code fails
        clock.advance(12.0)
        replay = client.post(
            "/api/v1/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dev_code,
            },
        )
        assert replay.status_code == 400

        # Denied & expired paths
        start_deny = client.post("/api/v1/auth/device/code", json={}).json()
        client.post(
            "/activate",
            data={
                CSRF_FORM_FIELD: csrf_val,
                "user_code": start_deny["user_code"],
                "decision": "deny",
            },
            headers={"Origin": "http://testserver"},
        )
        clock.advance(10.0)
        denied = client.post(
            "/api/v1/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": start_deny["device_code"],
            },
        )
        assert denied.json()["error"] == "access_denied"

        start_exp = client.post("/api/v1/auth/device/code", json={}).json()
        clock.advance(120.0)
        expired = client.post(
            "/api/v1/auth/device/token",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": start_exp["device_code"],
            },
        )
        assert expired.json()["error"] == "expired_token"


def test_in_app_and_web_account_deletion_purges_storage_and_sessions(tmp_path: Path) -> None:
    storage_dir = tmp_path / "storage"
    settings = make_cloud_settings(storage_dir)
    app = create_cloud_app(settings)

    with TestClient(app) as client:
        # 1. API DELETE /api/v1/auth/account (App Store Guideline 5.1.1(v))
        reg = client.post(
            "/api/v1/auth/register",
            json=auth_payload("delete-me@example.com", "ephemeral-phrase"),
        ).json()
        bearer = reg["access_token"]
        user_id = reg["user"]["user_id"]
        now = app.state.cloud.now()
        app.state.cloud.store.upsert_subscription(
            user_id,
            active=True,
            plan="pro",
            source="app_store",
            status="active",
            minutes_limit=300.0,
            now=now,
        )

        # Create a published review directory on disk so we prove storage purge
        report_payload = json.dumps({"video": "clip.mp4", "findings": []}).encode("utf-8")

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("clip_report.json")
            info.size = len(report_payload)
            tf.addfile(info, io.BytesIO(report_payload))
        manifest = {
            "schema_version": "publish_manifest.v1",
            "video_stem": "clip",
            "video_included": False,
            "visibility": "private",
        }
        pub = client.post(
            "/api/v1/reviews/publish",
            headers={"Authorization": f"Bearer {bearer}"},
            data={"manifest": json.dumps(manifest)},
            files={"bundle": ("bundle.tar.gz", tar_buf.getvalue(), "application/gzip")},
        )
        assert pub.status_code == 201
        share_id = pub.json()["share_id"]
        review_folder = settings.reviews_dir / share_id
        assert review_folder.is_dir()

        deleted = client.delete(
            "/api/v1/auth/account", headers={"Authorization": f"Bearer {bearer}"}
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "user_id": user_id}
        assert not review_folder.exists()
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bearer}"}).status_code
            == 401
        )
        assert client.get(f"/r/{share_id}").status_code == 404

        # 2. Web form POST /api/v1/auth/account/delete with CSRF
        client.post(
            "/api/v1/auth/register",
            json=auth_payload("web-delete@example.com", "ephemeral-phrase", cookie=True),
        )
        csrf_val = client.cookies.get(CSRF_COOKIE) or ""
        account_page = client.get("/account")
        assert account_page.status_code == 200
        assert "/api/v1/auth/account/delete" in account_page.text

        form_del = client.post(
            "/api/v1/auth/account/delete",
            data={CSRF_FORM_FIELD: csrf_val},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert form_del.status_code == 303
        assert client.get("/api/v1/auth/me").status_code == 401
