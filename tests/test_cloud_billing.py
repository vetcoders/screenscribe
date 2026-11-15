"""Cloud billing: StoreKit 2 x5c JWS, Stripe Checkout/Portal/Webhooks, 402 gate, and SQLite concurrency."""

from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from screenscribe.cloud.app import create_cloud_app
from screenscribe.cloud.db import NewJob
from screenscribe.cloud.jws import (
    AppleStoreKitVerifier,
    HmacStoreKitVerifier,
    JwsError,
    StaticKeyStoreKitVerifier,
    normalize_fingerprint,
    verify_x5c_chain,
)
from tests.cloud_test_helpers import (
    auth_payload,
    make_cloud_settings,
    make_ec_keypair,
    make_storekit_x5c_fixture,
    sign_es256_jws,
    sign_hs256_jws,
)


def _stripe_sig(webhook_key: str, timestamp: int, raw_body: bytes) -> str:
    digest = hmac.new(
        webhook_key.encode("utf-8"), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_storekit2_real_x5c_der_chain_and_appstore_sync(tmp_path: Path) -> None:
    x5c, root_fp, leaf_d = make_storekit_x5c_fixture()
    now_dt = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)
    now_ms = int(now_dt.timestamp() * 1000)

    # Direct x5c verification + StaticKey / HMAC verifiers
    leaf_key = verify_x5c_chain(
        x5c, trusted_root_fingerprints=(normalize_fingerprint(root_fp),), at=now_dt
    )
    assert leaf_key.curve == "P-256"

    static_pub, static_d = make_ec_keypair("P-256", 53)
    static_jws = sign_es256_jws({"alg": "ES256"}, {"ok": True}, static_d)
    assert StaticKeyStoreKitVerifier(static_pub).verify(static_jws) == {"ok": True}

    hmac_jws = sign_hs256_jws({"alg": "HS256"}, {"ok": 1}, b"test-hmac-bytes")
    assert HmacStoreKitVerifier(b"test-hmac-bytes").verify(hmac_jws) == {"ok": 1}

    with pytest.raises(JwsError):
        AppleStoreKitVerifier(trusted_root_fingerprints=("00" * 32,)).verify(
            sign_es256_jws({"alg": "ES256", "x5c": x5c}, {"signedDate": now_ms}, leaf_d)
        )

    settings = make_cloud_settings(
        tmp_path / "storage",
        apple_root_fingerprints=(root_fp,),
        appstore_allow_xcode=True,
    )
    app = create_cloud_app(settings, clock=lambda: now_dt)

    with TestClient(app) as client:
        u1 = client.post(
            "/api/v1/auth/register",
            json=auth_payload("buyer1@example.com", "buyer-one-phrase"),
        ).json()
        u2 = client.post(
            "/api/v1/auth/register",
            json=auth_payload("buyer2@example.com", "buyer-two-phrase"),
        ).json()
        b1 = u1["access_token"]
        b2 = u2["access_token"]

        tx_claims = {
            "bundleId": "io.vetcoders.screenscribe.mac",
            "productId": "io.vetcoders.screenscribe.pro.monthly",
            "originalTransactionId": "orig-tx-9001",
            "transactionId": "tx-9001",
            "purchaseDate": now_ms - 60_000,
            "expiresDate": now_ms + 30 * 86_400_000,
            "signedDate": now_ms,
            "environment": "Sandbox",
        }
        renewal_claims = {
            "originalTransactionId": "orig-tx-9001",
            "autoRenewStatus": 1,
            "signedDate": now_ms,
        }
        signed_tx = sign_es256_jws({"alg": "ES256", "x5c": x5c}, tx_claims, leaf_d)
        signed_ren = sign_es256_jws({"alg": "ES256", "x5c": x5c}, renewal_claims, leaf_d)

        synced = client.post(
            "/api/v1/billing/appstore/sync",
            headers={"Authorization": f"Bearer {b1}"},
            json={
                "signed_transaction_info": signed_tx,
                "signed_renewal_info": signed_ren,
                "environment": "Sandbox",
            },
        )
        assert synced.status_code == 200
        sub = synced.json()["subscription"]
        assert sub["active"] is True
        assert sub["plan"] == "pro"
        assert sub["source"] == "app_store"
        assert sub["minutes_limit"] == 300.0

        # Another user attempting to claim the same originalTransactionId gets 409
        hijack = client.post(
            "/api/v1/billing/appstore/sync",
            headers={"Authorization": f"Bearer {b2}"},
            json={"signed_transaction_info": signed_tx, "environment": "Sandbox"},
        )
        assert hijack.status_code == 409

        # Wrong bundleId gets 400
        wrong_bundle = sign_es256_jws(
            {"alg": "ES256", "x5c": x5c},
            {**tx_claims, "bundleId": "com.other.app"},
            leaf_d,
        )
        res_wrong = client.post(
            "/api/v1/billing/appstore/sync",
            headers={"Authorization": f"Bearer {b1}"},
            json={"signed_transaction_info": wrong_bundle, "environment": "Sandbox"},
        )
        assert res_wrong.status_code == 400

        # Revocation deactivates entitlement immediately
        revoked_tx = sign_es256_jws(
            {"alg": "ES256", "x5c": x5c},
            {**tx_claims, "revocationDate": now_ms},
            leaf_d,
        )
        revoked_res = client.post(
            "/api/v1/billing/appstore/sync",
            headers={"Authorization": f"Bearer {b1}"},
            json={"signed_transaction_info": revoked_tx, "environment": "Sandbox"},
        )
        assert revoked_res.status_code == 200
        assert revoked_res.json()["subscription"]["active"] is False


def test_stripe_checkout_portal_and_signed_webhooks_with_replay_and_dedup(tmp_path: Path) -> None:
    now_dt = datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
    now_ts = int(now_dt.timestamp())

    seen_forms: list[tuple[str, dict[str, list[str]]]] = []

    def stripe_handler(request: httpx.Request) -> httpx.Response:
        parsed = parse_qs(request.content.decode("utf-8"))
        seen_forms.append((request.url.path, parsed))
        if request.url.path == "/v1/customers":
            return httpx.Response(200, json={"id": "cus_test_123"})
        if request.url.path == "/v1/checkout/sessions":
            return httpx.Response(
                200,
                json={
                    "id": "cs_test_456",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_456",
                },
            )
        if request.url.path == "/v1/billing_portal/sessions":
            return httpx.Response(
                200, json={"url": "https://billing.stripe.com/p/session/bps_123"}
            )
        if request.url.path.startswith("/v1/subscriptions/"):
            return httpx.Response(
                200,
                json={
                    "id": "sub_stripe_789",
                    "status": "active",
                    "customer": "cus_test_123",
                    "current_period_start": now_ts,
                    "current_period_end": now_ts + 30 * 86400,
                    "items": {"data": [{"price": {"id": "price_pro_test"}}]},
                },
            )
        return httpx.Response(404, json={"error": "not_found"})

    transport = httpx.MockTransport(stripe_handler)
    settings = make_cloud_settings(tmp_path / "storage")
    wh_key = settings.stripe_webhook_secret
    app = create_cloud_app(settings, http_transport=transport, clock=lambda: now_dt)

    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json=auth_payload("stripe@example.com", "stripe-customer-phrase"),
        ).json()
        bearer = reg["access_token"]
        user_id = reg["user"]["user_id"]

        # Portal before customer exists -> 409
        no_portal = client.post(
            "/api/v1/billing/stripe/portal",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert no_portal.status_code == 409

        # Checkout creates Stripe customer + checkout session
        checkout = client.post(
            "/api/v1/billing/stripe/checkout",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"plan": "pro"},
        )
        assert checkout.status_code == 200
        assert checkout.json()["session_id"] == "cs_test_456"

        # Billing portal now succeeds
        portal = client.post(
            "/api/v1/billing/stripe/portal",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert portal.status_code == 200
        assert "billing.stripe.com" in portal.json()["portal_url"]

        # Webhook: invalid signature & stale timestamp (>300s) -> 400
        event_1 = {
            "id": "evt_checkout_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "subscription",
                    "client_reference_id": user_id,
                    "customer": "cus_test_123",
                    "subscription": "sub_stripe_789",
                    "metadata": {"user_id": user_id, "plan": "pro"},
                }
            },
        }
        raw_1 = json.dumps(event_1).encode("utf-8")

        bad_sig = client.post(
            "/api/v1/billing/stripe/webhook",
            content=raw_1,
            headers={"Stripe-Signature": f"t={now_ts},v1=deadbeef"},
        )
        assert bad_sig.status_code == 400

        stale_sig = client.post(
            "/api/v1/billing/stripe/webhook",
            content=raw_1,
            headers={"Stripe-Signature": _stripe_sig(wh_key, now_ts - 600, raw_1)},
        )
        assert stale_sig.status_code == 400

        # Valid webhook activates subscription; duplicate replay is idempotent
        ok_wh = client.post(
            "/api/v1/billing/stripe/webhook",
            content=raw_1,
            headers={"Stripe-Signature": _stripe_sig(wh_key, now_ts, raw_1)},
        )
        assert ok_wh.status_code == 200
        assert ok_wh.json() == {"received": True}

        dup_wh = client.post(
            "/api/v1/billing/stripe/webhook",
            content=raw_1,
            headers={"Stripe-Signature": _stripe_sig(wh_key, now_ts, raw_1)},
        )
        assert dup_wh.status_code == 200
        assert dup_wh.json() == {"received": True, "duplicate": True}

        me_active = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {bearer}"}
        ).json()
        assert me_active["subscription"]["active"] is True
        assert me_active["subscription"]["plan"] == "pro"

        # invoice.payment_failed -> past_due / inactive
        event_fail = {
            "id": "evt_inv_fail_2",
            "type": "invoice.payment_failed",
            "data": {"object": {"subscription": "sub_stripe_789"}},
        }
        raw_fail = json.dumps(event_fail).encode("utf-8")
        res_fail = client.post(
            "/api/v1/billing/stripe/webhook",
            content=raw_fail,
            headers={"Stripe-Signature": _stripe_sig(wh_key, now_ts, raw_fail)},
        )
        assert res_fail.status_code == 200
        me_past_due = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {bearer}"}
        ).json()
        assert me_past_due["subscription"]["active"] is False

        # customer.subscription.deleted -> canceled
        event_del = {
            "id": "evt_sub_del_3",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_stripe_789"}},
        }
        raw_del = json.dumps(event_del).encode("utf-8")
        assert (
            client.post(
                "/api/v1/billing/stripe/webhook",
                content=raw_del,
                headers={"Stripe-Signature": _stripe_sig(wh_key, now_ts, raw_del)},
            ).status_code
            == 200
        )


def test_402_gate_and_concurrent_sqlite_quota_reservation_and_refund(tmp_path: Path) -> None:
    now_dt = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings, duration_probe=lambda _p: 360.0, clock=lambda: now_dt)

    with TestClient(app) as client:
        reg = client.post(
            "/api/v1/auth/register",
            json=auth_payload("quota@example.com", "quota-user-phrase"),
        ).json()
        bearer = reg["access_token"]
        user_id = reg["user"]["user_id"]

        # Without subscription -> 402 no_active_subscription
        res_no_sub = client.post(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {bearer}"},
            files={"video": ("demo.mp4", b"fake-mp4-bytes", "video/mp4")},
        )
        assert res_no_sub.status_code == 402
        body_402 = res_no_sub.json()
        assert body_402["error"] == "subscription_required"
        assert body_402["code"] == "no_active_subscription"
        assert body_402["upgrade_url"] == "http://testserver/billing"

        # Seed a 10-minute plan and race 5 concurrent 6-minute reservations
        store = app.state.cloud.store
        store.upsert_subscription(
            user_id,
            active=True,
            plan="starter",
            source="stripe",
            status="active",
            minutes_limit=10.0,
            now=now_dt,
            period_start="2026-07-01T00:00:00Z",
            period_end="2026-08-01T00:00:00Z",
        )

        def reserve_one(idx: int) -> str:
            job = NewJob(
                job_id=f"job_race{idx:04d}",
                user_id=user_id,
                video_filename="race.mp4",
                video_path=str(tmp_path / f"race_{idx}.mp4"),
                duration_seconds=360.0,
                language="en",
                preset="programming",
                prompt_override=None,
                visibility="private",
                idempotency_key=f"race-key-{idx}",
            )
            return store.reserve_job(job, 6.0, now_dt).status

        with ThreadPoolExecutor(max_workers=5) as pool:
            outcomes = list(pool.map(reserve_one, range(5)))

        assert outcomes.count("ok") == 1
        assert outcomes.count("quota_exceeded") == 4

        # Next HTTP upload also hits 402 quota_exceeded
        res_over = client.post(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {bearer}"},
            files={"video": ("demo.mp4", b"fake-mp4-bytes", "video/mp4")},
        )
        assert res_over.status_code == 402
        assert res_over.json()["code"] == "quota_exceeded"

        # Refunding the reserved job restores full 10.0 remaining minutes (and is idempotent)
        winner_jobs = store.list_jobs(user_id)
        assert len(winner_jobs) == 1
        store.refund_job(winner_jobs[0].job_id, now_dt)
        store.refund_job(winner_jobs[0].job_id, now_dt)
        snap = store.subscription_snapshot(user_id, now_dt)
        assert snap["minutes_used"] == 0.0
        assert snap["minutes_remaining"] == 10.0
