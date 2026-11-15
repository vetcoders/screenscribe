"""ASGI application factory for Screenscribe Cloud.

``create_cloud_app()`` wires settings, the SQLite store, verifiers and every
router (auth, billing, jobs, reviews, hosted review, portal) behind:

- a pure-ASGI request-body limiter with per-route caps (early 413 from
  ``Content-Length`` and a running byte count for chunked bodies);
- security headers (default CSP, ``nosniff``, frame denial, HSTS on HTTPS);
- explicit-origin CORS only when ``SCREENSCRIBE_CLOUD_CORS_ORIGINS`` is set;
- ``TrustedHostMiddleware`` when ``SCREENSCRIBE_CLOUD_TRUSTED_HOSTS`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from screenscribe import __version__
from screenscribe.server_common import MAX_AUDIO_BYTES

from .auth import CloudServices, build_auth_router
from .billing import SubscriptionRequired, build_billing_router
from .config import CloudSettings
from .db import CloudStore
from .jobs import MULTIPART_OVERHEAD_BYTES, build_jobs_router
from .jws import AppleJwksVerifier, AppleJwsVerifier, AppleStoreKitVerifier, StoreKitVerifier
from .portal import PORTAL_CSP, build_portal_router
from .publish import build_reviews_router
from .review_host import ReviewRegistry, build_review_host_router
from .schemas import subscription_required_body
from .security import utc_now
from .worker import JobWorker, PipelineRunner

logger = logging.getLogger(__name__)

DEFAULT_BODY_LIMIT = 32 * 1024 * 1024
WEBHOOK_BODY_LIMIT = 1024 * 1024
_AUDIO_ROUTE_RE = re.compile(r"^/r/[^/]+/api/(?:agent/)?stt$")
_WEBHOOK_PATH = "/api/v1/billing/stripe/webhook"


# -- Middleware ---------------------------------------------------------------


class BodyLimitMiddleware:
    """Reject request bodies above a per-route cap before handlers buffer them."""

    def __init__(self, app: ASGIApp, *, limit_for: Callable[[str, str], int]) -> None:
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("method", "GET").upper(), scope.get("path", ""))
        declared: int | None = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            response = JSONResponse(status_code=413, content={"detail": "Request body too large."})
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise StarletteHTTPException(status_code=413, detail="Request body too large.")
            return message

        await self.app(scope, limited_receive, send)


class SecurityHeadersMiddleware:
    """Baseline response headers; routes may set a stricter or route-specific CSP."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"permissions-policy", b"camera=(), geolocation=(), payment=(), microphone=(self)"),
                    (b"content-security-policy", PORTAL_CSP.encode("ascii")),
                ]
                if self.hsts:
                    defaults.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                headers.extend((name, value) for name, value in defaults if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def body_limits(settings: CloudSettings) -> Callable[[str, str], int]:
    jobs_cap = settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
    publish_cap = settings.max_upload_bytes + settings.max_bundle_bytes + MULTIPART_OVERHEAD_BYTES
    audio_cap = MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES

    def limit_for(method: str, path: str) -> int:
        if method == "POST":
            if path == "/api/v1/jobs":
                return jobs_cap
            if path == "/api/v1/reviews/publish":
                return publish_cap
            if path == _WEBHOOK_PATH:
                return WEBHOOK_BODY_LIMIT
            if _AUDIO_ROUTE_RE.match(path):
                return audio_cap
        return DEFAULT_BODY_LIMIT

    return limit_for


def allowed_hosts(settings: CloudSettings) -> list[str]:
    hosts = {"localhost", "127.0.0.1"}  # container healthcheck
    public = urlsplit(settings.public_base).hostname
    if public:
        hosts.add(public.lower())
    hosts.update(host.lower().split(":", 1)[0] for host in settings.trusted_hosts if host)
    return sorted(hosts)


# -- Health -------------------------------------------------------------------


def readiness(services: CloudServices) -> tuple[bool, dict[str, Any]]:
    settings = services.settings
    checks: dict[str, Any] = {}
    try:
        services.store.ping()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    storage_ok = all(
        path.is_dir() and os.access(path, os.W_OK)
        for path in (settings.uploads_dir, settings.reviews_dir)
    )
    checks["storage"] = "ok" if storage_ok else "error"
    checks["ai_capacity"] = settings.has_ai_capacity
    checks["stripe"] = settings.stripe_enabled
    checks["app_store"] = bool(settings.apple_bundle_id)
    ready = checks["database"] == "ok" and storage_ok
    return ready, checks


def ensure_storage(settings: CloudSettings) -> None:
    for path in (settings.storage_dir, settings.uploads_dir, settings.reviews_dir, settings.glossary_root):
        Path(path).mkdir(parents=True, exist_ok=True)


# -- Factory ------------------------------------------------------------------


def create_cloud_app(
    settings: CloudSettings | None = None,
    *,
    store: CloudStore | None = None,
    apple_verifier: AppleJwsVerifier | None = None,
    storekit_verifier: StoreKitVerifier | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    duration_probe: Callable[[Path], float] | None = None,
    pipeline_runner: PipelineRunner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the hosted backend. Every collaborator is injectable for tests."""
    cfg = settings or CloudSettings.from_env()
    owns_store = store is None
    cloud_store = store or CloudStore(cfg.database_file)
    now = clock or utc_now
    services = CloudServices(
        settings=cfg,
        store=cloud_store,
        apple_verifier=apple_verifier
        or AppleJwksVerifier(cfg.apple_jwks_url, transport=http_transport),
        storekit_verifier=storekit_verifier
        or (
            AppleStoreKitVerifier(cfg.apple_root_fingerprints)
            if cfg.apple_root_fingerprints
            else AppleStoreKitVerifier()
        ),
        http_transport=http_transport,
        duration_probe=duration_probe,
        clock=now,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(ensure_storage, cfg)
        await run_in_threadpool(cloud_store.init_schema, now())
        stop = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        if cfg.embedded_worker:
            worker = JobWorker(cfg, cloud_store, runner=pipeline_runner, clock=now)
            worker_task = asyncio.create_task(worker.run(stop), name="screenscribe-cloud-worker")
            logger.info("Embedded job worker started")
        try:
            yield
        finally:
            stop.set()
            if worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            if owns_store:
                cloud_store.close()

    app = FastAPI(
        title="Screenscribe Cloud",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.cloud = services
    app.state.review_registry = ReviewRegistry()

    @app.exception_handler(SubscriptionRequired)
    async def subscription_required(request: Request, exc: SubscriptionRequired) -> JSONResponse:
        return JSONResponse(
            status_code=402,
            content=subscription_required_body(exc.code, cfg.url("/billing")),
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        ready, checks = await run_in_threadpool(readiness, services)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
            headers={"Cache-Control": "no-store"},
        )

    app.include_router(build_auth_router())
    app.include_router(build_billing_router())
    app.include_router(build_jobs_router())
    app.include_router(build_reviews_router())
    app.include_router(build_review_host_router())
    app.include_router(build_portal_router())

    # Added innermost-first: the body limiter runs closest to the routes.
    app.add_middleware(BodyLimitMiddleware, limit_for=body_limits(cfg))
    app.add_middleware(SecurityHeadersMiddleware, hsts=cfg.secure_cookies)
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
            max_age=600,
        )
    if cfg.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(cfg))
    return app


__all__ = [
    "DEFAULT_BODY_LIMIT",
    "WEBHOOK_BODY_LIMIT",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
    "allowed_hosts",
    "body_limits",
    "create_cloud_app",
    "ensure_storage",
    "readiness",
]
