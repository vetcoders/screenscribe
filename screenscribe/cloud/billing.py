"""Hard subscription: Stripe (web) + StoreKit 2 (Mac App Store), one ledger.

Both billing sources converge on the single ``subscriptions`` row per user
and the ``usage_ledger`` minute balance in :mod:`screenscribe.cloud.db`.
Stripe is spoken to directly over HTTPS with a finite ``httpx`` timeout (no
SDK dependency); StoreKit 2 signed transactions are verified through the
pluggable :class:`~screenscribe.cloud.jws.StoreKitVerifier`.

ZERO BYOK: nothing here (or anywhere in the cloud API) accepts an AI
provider credential from a customer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from typing import Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .auth import CloudServices, CurrentAuth, get_services, me_body, read_form_or_json
from .db import SubscriptionRecord, UserRecord
from .jws import JwsError
from .schemas import AppStoreSyncRequest, CheckoutRequest, GateCode
from .security import from_epoch, isoformat_z, safe_next_path

logger = logging.getLogger(__name__)

STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300
HANDLED_STRIPE_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)
APPSTORE_ENVIRONMENTS = frozenset({"Production", "Sandbox"})


# -- 402 gate ----------------------------------------------------------------


class SubscriptionRequired(Exception):
    """Raised by gated routes; the app renders the exact 402 contract body."""

    def __init__(self, code: GateCode) -> None:
        super().__init__(code)
        self.code: GateCode = code


def require_active_subscription(
    services: CloudServices, user_id: str, required_minutes: float = 0.0
) -> None:
    """Raise :class:`SubscriptionRequired` unless the user may spend AI minutes."""
    code = services.store.check_gate(user_id, services.now(), required_minutes)
    if code is not None:
        raise SubscriptionRequired(code)


# -- Stripe signature --------------------------------------------------------


def verify_stripe_signature(
    secret: str,
    header: str | None,
    payload: bytes,
    *,
    now: float | None = None,
    tolerance: int = STRIPE_SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Verify ``Stripe-Signature: t=<unix>,v1=<hex>[,v1=...]`` (HMAC-SHA256).

    The signed payload is ``f"{t}.".encode() + raw_body``; the timestamp must
    be within ``tolerance`` seconds of ``now`` (replay guard) and at least one
    ``v1`` signature must match in constant time.
    """
    if not secret or not header:
        return False
    timestamps: list[int] = []
    signatures: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            try:
                timestamps.append(int(value))
            except ValueError:
                continue
        elif key == "v1" and value:
            signatures.append(value.strip())
    if len(timestamps) != 1 or not signatures:
        return False
    stamp = timestamps[0]
    current = time.time() if now is None else now
    if abs(current - stamp) > tolerance:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), f"{stamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


# -- Stripe API client (form-encoded REST, finite timeout) ------------------


class StripeUnavailable(Exception):
    """Stripe returned an error or could not be reached."""


async def stripe_request(
    services: CloudServices,
    method: str,
    path: str,
    form: list[tuple[str, str]] | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    settings = services.settings
    if not settings.stripe_enabled:
        raise HTTPException(status_code=503, detail="Stripe billing is not configured.")
    headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    url = f"{settings.stripe_api_base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), transport=services.http_transport
        ) as client:
            response = await client.request(method, url, data=dict(form or []), headers=headers)
    except httpx.HTTPError as exc:
        raise StripeUnavailable(type(exc).__name__) from exc
    if response.status_code >= 400:
        raise StripeUnavailable(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise StripeUnavailable("invalid JSON") from exc
    if not isinstance(body, dict):
        raise StripeUnavailable("unexpected body")
    return cast(dict[str, Any], body)


def _stripe_error(exc: StripeUnavailable) -> HTTPException:
    logger.warning("Stripe request failed: %s", exc)
    return HTTPException(status_code=502, detail="Payment provider is unavailable. Try again.")


async def ensure_stripe_customer(services: CloudServices, user: UserRecord) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = await stripe_request(
        services,
        "POST",
        "/v1/customers",
        [("email", user.email), ("metadata[user_id]", user.user_id)],
        idempotency_key=f"customer-{user.user_id}",
    )
    customer_id = str(customer.get("id") or "")
    if not customer_id:
        raise StripeUnavailable("customer without id")
    await run_in_threadpool(
        services.store.set_stripe_customer, user.user_id, customer_id, services.now()
    )
    return customer_id


# -- Stripe webhook event handling -------------------------------------------


def _obj(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    if isinstance(data, dict) and isinstance(data.get("object"), dict):
        return cast(dict[str, Any], data["object"])
    return {}


def _meta(obj: dict[str, Any]) -> dict[str, Any]:
    meta = obj.get("metadata")
    return cast(dict[str, Any], meta) if isinstance(meta, dict) else {}


def _first_item(subscription: dict[str, Any]) -> dict[str, Any]:
    items = subscription.get("items")
    if isinstance(items, dict):
        data = items.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return cast(dict[str, Any], data[0])
    return {}


def _period_bounds(subscription: dict[str, Any]) -> tuple[str | None, str | None]:
    """Current period from the subscription (older API) or its first item (newer API)."""
    item = _first_item(subscription)
    start = subscription.get("current_period_start", item.get("current_period_start"))
    end = subscription.get("current_period_end", item.get("current_period_end"))
    start_iso = isoformat_z(from_epoch(float(start))) if isinstance(start, int | float) else None
    end_iso = isoformat_z(from_epoch(float(end))) if isinstance(end, int | float) else None
    return start_iso, end_iso


def _plan_for_subscription(services: CloudServices, subscription: dict[str, Any]) -> str | None:
    price = _first_item(subscription).get("price")
    if isinstance(price, dict):
        plan = services.settings.plan_for_price(str(price.get("id") or ""))
        if plan:
            return plan
    plan_hint = str(_meta(subscription).get("plan") or "")
    return plan_hint if plan_hint in services.settings.plan_minutes else None


def _user_for_stripe(
    services: CloudServices, obj: dict[str, Any], *, user_hint: str | None = None
) -> UserRecord | None:
    store = services.store
    for candidate in (user_hint, str(_meta(obj).get("user_id") or "")):
        if candidate:
            user = store.get_user(candidate)
            if user is not None:
                return user
    customer = obj.get("customer")
    customer_id = customer if isinstance(customer, str) else ""
    return store.get_user_by_stripe_customer(customer_id) if customer_id else None


def apply_stripe_subscription(
    services: CloudServices,
    subscription: dict[str, Any],
    *,
    user_hint: str | None = None,
    plan_hint: str | None = None,
) -> SubscriptionRecord | None:
    """Mirror one Stripe subscription object onto the local subscription row."""
    user = _user_for_stripe(services, subscription, user_hint=user_hint)
    if user is None:
        logger.warning("Stripe subscription for an unknown customer ignored.")
        return None
    store = services.store
    now = services.now()
    customer = subscription.get("customer")
    if isinstance(customer, str) and customer and user.stripe_customer_id != customer:
        store.set_stripe_customer(user.user_id, customer, now)
    status = str(subscription.get("status") or "inactive")
    plan = _plan_for_subscription(services, subscription) or plan_hint or "pro"
    period_start, period_end = _period_bounds(subscription)
    if period_start is None:
        period_start = isoformat_z(now)
    if period_end is None:
        period_end = isoformat_z(now + timedelta(days=31))
    active = status in {"active", "trialing"}
    return store.upsert_subscription(
        user.user_id,
        active=active,
        plan=plan,
        source="stripe",
        status=status if status in {"active", "trialing", "past_due", "canceled"} else "inactive",
        minutes_limit=services.settings.minutes_for_plan(plan),
        now=now,
        external_subscription_id=str(subscription.get("id") or "") or None,
        period_start=period_start,
        period_end=period_end,
    )


def _invoice_subscription_id(invoice: dict[str, Any]) -> str:
    direct = invoice.get("subscription")
    if isinstance(direct, str):
        return direct
    parent = invoice.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_details")
        if isinstance(details, dict) and isinstance(details.get("subscription"), str):
            return str(details["subscription"])
    return ""


async def handle_stripe_event(services: CloudServices, event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "")
    obj = _obj(event)
    if event_type == "checkout.session.completed":
        if obj.get("mode") not in (None, "subscription"):
            return
        user_hint = str(obj.get("client_reference_id") or _meta(obj).get("user_id") or "")
        plan_hint = str(_meta(obj).get("plan") or "") or None
        subscription_ref = obj.get("subscription")
        subscription: dict[str, Any] | None = None
        if isinstance(subscription_ref, dict):
            subscription = cast(dict[str, Any], subscription_ref)
        elif isinstance(subscription_ref, str) and subscription_ref:
            try:
                subscription = await stripe_request(
                    services, "GET", f"/v1/subscriptions/{subscription_ref}"
                )
            except (StripeUnavailable, HTTPException):
                subscription = {
                    "id": subscription_ref,
                    "status": "active",
                    "customer": obj.get("customer"),
                    "metadata": _meta(obj),
                }
        if subscription is None:
            return
        if not subscription.get("customer"):
            subscription = {**subscription, "customer": obj.get("customer")}
        sub_payload: dict[str, Any] = subscription
        await run_in_threadpool(
            lambda: apply_stripe_subscription(
                services, sub_payload, user_hint=user_hint, plan_hint=plan_hint
            )
        )
    elif event_type in {"customer.subscription.created", "customer.subscription.updated"}:
        await run_in_threadpool(apply_stripe_subscription, services, obj)
    elif event_type == "customer.subscription.deleted":
        record = await run_in_threadpool(
            services.store.find_subscription_by_external_id, str(obj.get("id") or "")
        )
        if record is not None:
            deleted_uid = record.user_id
            await run_in_threadpool(
                lambda: services.store.set_subscription_state(
                    deleted_uid, active=False, status="canceled", now=services.now()
                )
            )
    elif event_type == "invoice.payment_failed":
        record = await run_in_threadpool(
            services.store.find_subscription_by_external_id, _invoice_subscription_id(obj)
        )
        if record is not None:
            failed_uid = record.user_id
            await run_in_threadpool(
                lambda: services.store.set_subscription_state(
                    failed_uid, active=False, status="past_due", now=services.now()
                )
            )


# -- StoreKit 2 --------------------------------------------------------------


def _ms_to_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return isoformat_z(from_epoch(float(value) / 1000.0))


def sync_app_store(
    services: CloudServices, user: UserRecord, payload: AppStoreSyncRequest
) -> dict[str, Any]:
    """Verify a StoreKit 2 signed transaction and upsert the entitlement."""
    settings = services.settings
    environment = payload.environment.strip() or "Production"
    if environment not in APPSTORE_ENVIRONMENTS and not (
        environment == "Xcode" and settings.appstore_allow_xcode
    ):
        raise HTTPException(status_code=400, detail="Unsupported App Store environment.")
    if not settings.apple_bundle_id:
        raise HTTPException(status_code=503, detail="App Store billing is not configured.")
    try:
        transaction = services.storekit_verifier.verify(payload.signed_transaction_info)
        renewal = (
            services.storekit_verifier.verify(payload.signed_renewal_info)
            if payload.signed_renewal_info
            else None
        )
    except JwsError as exc:
        logger.info("StoreKit transaction rejected: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid StoreKit signed transaction.") from exc
    if str(transaction.get("bundleId") or "") != settings.apple_bundle_id:
        raise HTTPException(status_code=400, detail="Transaction belongs to another app.")
    tx_environment = str(transaction.get("environment") or environment)
    if tx_environment != environment:
        raise HTTPException(status_code=400, detail="Transaction environment mismatch.")
    original_tx = str(
        transaction.get("originalTransactionId") or transaction.get("transactionId") or ""
    )
    if not original_tx:
        raise HTTPException(status_code=400, detail="Transaction has no identifier.")
    if renewal is not None and str(renewal.get("originalTransactionId") or "") not in (
        "",
        original_tx,
    ):
        raise HTTPException(status_code=400, detail="Renewal info does not match the transaction.")
    product_id = str(transaction.get("productId") or "")
    plan = settings.appstore_products.get(product_id)
    if plan is None:
        raise HTTPException(status_code=400, detail="Unknown App Store product.")
    store = services.store
    bound = store.find_subscription_by_original_transaction(original_tx)
    if bound is not None and bound.user_id != user.user_id:
        raise HTTPException(status_code=409, detail="This purchase is linked to another account.")
    now = services.now()
    expires_iso = _ms_to_iso(transaction.get("expiresDate"))
    expires_ms = transaction.get("expiresDate")
    revoked = transaction.get("revocationDate") is not None
    expired = (
        not isinstance(expires_ms, int | float)
        or isinstance(expires_ms, bool)
        or float(expires_ms) <= now.timestamp() * 1000.0
    )
    active = not revoked and not expired
    status = "active" if active else ("canceled" if revoked else "expired")
    period_start = _ms_to_iso(transaction.get("purchaseDate")) or isoformat_z(now)
    store.upsert_subscription(
        user.user_id,
        active=active,
        plan=plan,
        source="app_store",
        status=status,
        minutes_limit=settings.minutes_for_plan(plan),
        now=now,
        original_transaction_id=original_tx,
        environment=environment,
        period_start=period_start,
        period_end=expires_iso,
    )
    return me_body(services, user)


# -- Router ------------------------------------------------------------------


def build_billing_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

    @router.post("/stripe/checkout")
    async def stripe_checkout(request: Request, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        data = await read_form_or_json(request)
        try:
            payload = CheckoutRequest.model_validate(dict(data))
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        price = services.settings.stripe_prices.get(payload.plan)
        if not price:
            raise HTTPException(status_code=400, detail="Unknown subscription plan.")
        success = safe_next_path(payload.success_path, "/billing?checkout=success")
        cancel = safe_next_path(payload.cancel_path, "/billing?checkout=cancel")
        try:
            customer_id = await ensure_stripe_customer(services, auth.user)
            session = await stripe_request(
                services,
                "POST",
                "/v1/checkout/sessions",
                [
                    ("mode", "subscription"),
                    ("customer", customer_id),
                    ("client_reference_id", auth.user.user_id),
                    ("line_items[0][price]", price),
                    ("line_items[0][quantity]", "1"),
                    ("success_url", services.settings.url(success)),
                    ("cancel_url", services.settings.url(cancel)),
                    ("allow_promotion_codes", "true"),
                    ("metadata[user_id]", auth.user.user_id),
                    ("metadata[plan]", payload.plan),
                    ("subscription_data[metadata][user_id]", auth.user.user_id),
                    ("subscription_data[metadata][plan]", payload.plan),
                ],
            )
        except StripeUnavailable as exc:
            raise _stripe_error(exc) from exc
        checkout_url = str(session.get("url") or "")
        session_id = str(session.get("id") or "")
        if not checkout_url or not session_id:
            raise _stripe_error(StripeUnavailable("session without url"))
        return {"checkout_url": checkout_url, "session_id": session_id}

    @router.post("/stripe/portal")
    async def stripe_portal(request: Request, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        if not auth.user.stripe_customer_id:
            raise HTTPException(status_code=409, detail="No Stripe billing account yet.")
        try:
            session = await stripe_request(
                services,
                "POST",
                "/v1/billing_portal/sessions",
                [
                    ("customer", auth.user.stripe_customer_id),
                    ("return_url", services.settings.url("/billing")),
                ],
            )
        except StripeUnavailable as exc:
            raise _stripe_error(exc) from exc
        portal_url = str(session.get("url") or "")
        if not portal_url:
            raise _stripe_error(StripeUnavailable("portal without url"))
        return {"portal_url": portal_url}

    @router.post("/stripe/webhook")
    async def stripe_webhook(request: Request) -> Response:
        services = get_services(request)
        secret = services.settings.stripe_webhook_secret
        if not secret:
            raise HTTPException(status_code=503, detail="Stripe webhook is not configured.")
        raw = await request.body()
        if not verify_stripe_signature(
            secret,
            request.headers.get("stripe-signature"),
            raw,
            now=services.now().timestamp(),
        ):
            raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Malformed webhook payload.") from exc
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            raise HTTPException(status_code=400, detail="Malformed webhook payload.")
        event_id = str(event["id"])
        event_type = str(event.get("type") or "")
        if await run_in_threadpool(services.store.stripe_event_seen, event_id):
            return JSONResponse(content={"received": True, "duplicate": True})
        if event_type in HANDLED_STRIPE_EVENTS:
            await handle_stripe_event(services, cast(dict[str, Any], event))
        await run_in_threadpool(
            services.store.mark_stripe_event, event_id, event_type, services.now()
        )
        return JSONResponse(content={"received": True})

    @router.post("/appstore/sync")
    async def appstore_sync(request: Request, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        data = await read_form_or_json(request)
        try:
            payload = AppStoreSyncRequest.model_validate(dict(data))
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        return await run_in_threadpool(sync_app_store, services, auth.user, payload)

    return router


__all__ = [
    "HANDLED_STRIPE_EVENTS",
    "STRIPE_SIGNATURE_TOLERANCE_SECONDS",
    "StripeUnavailable",
    "SubscriptionRequired",
    "apply_stripe_subscription",
    "build_billing_router",
    "handle_stripe_event",
    "require_active_subscription",
    "stripe_request",
    "sync_app_store",
    "verify_stripe_signature",
]
