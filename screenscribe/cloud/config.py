"""Server-side settings for Screenscribe Cloud.

Everything is read from the server environment. Customers never supply AI
provider credentials (ZERO BYOK): the pipeline runs on the server-owned
``SCREENSCRIBE_API_KEY`` / ``LIBRAXIS_API_KEY`` capacity configured here.

The Python default bind address is loopback. Container deployments widen it
explicitly through ``SCREENSCRIBE_CLOUD_HOST`` in the compose file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_STORAGE_DIR = ".screenscribe-cloud"
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_BUNDLE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 500
DEFAULT_MAX_ARCHIVE_TOTAL_BYTES = 1024 * 1024 * 1024
DEFAULT_REFRESH_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_WEB_SESSION_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_DEVICE_CODE_TTL_SECONDS = 600
DEFAULT_DEVICE_POLL_INTERVAL = 5
DEFAULT_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
DEFAULT_STRIPE_API_BASE = "https://api.stripe.com"
DEFAULT_PLAN_MINUTES: Mapping[str, float] = {"starter": 60.0, "pro": 300.0, "team": 1000.0}

_TRUE = frozenset({"1", "true", "yes", "on"})


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE


def _int(value: str | None, default: int, *, minimum: int = 0) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return max(minimum, parsed)


def _float(value: str | None, default: float, *, minimum: float = 0.0) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError:
        return default
    return max(minimum, parsed)


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _pairs(value: str | None) -> dict[str, str]:
    """Parse ``key=value,key2=value2`` into a dict (blank entries ignored)."""
    result: dict[str, str] = {}
    for item in _csv(value):
        key, sep, raw = item.partition("=")
        if sep and key.strip() and raw.strip():
            result[key.strip()] = raw.strip()
    return result


def _first(environ: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return ""


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class CloudSettings:
    """Immutable runtime configuration for the hosted backend and worker."""

    base_url: str = DEFAULT_BASE_URL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    storage_dir: Path = Path(DEFAULT_STORAGE_DIR)
    database_path: Path | None = None
    libraxis_api_key: str = field(default="", repr=False)
    api_base: str | None = None
    stt_endpoint: str | None = None
    llm_endpoint: str | None = None
    vision_endpoint: str | None = None
    llm_model: str | None = None
    vision_model: str | None = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    max_archive_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS
    max_archive_total_bytes: int = DEFAULT_MAX_ARCHIVE_TOTAL_BYTES
    min_job_minutes: float = 0.25
    refresh_ttl_seconds: int = DEFAULT_REFRESH_TTL_SECONDS
    web_session_ttl_seconds: int = DEFAULT_WEB_SESSION_TTL_SECONDS
    device_code_ttl_seconds: int = DEFAULT_DEVICE_CODE_TTL_SECONDS
    device_poll_interval: int = DEFAULT_DEVICE_POLL_INTERVAL
    cookie_secure: bool = False
    cors_origins: tuple[str, ...] = ()
    trusted_hosts: tuple[str, ...] = ()
    stripe_secret_key: str = field(default="", repr=False)
    stripe_webhook_secret: str = field(default="", repr=False)
    stripe_api_base: str = DEFAULT_STRIPE_API_BASE
    stripe_prices: Mapping[str, str] = field(default_factory=dict)
    apple_bundle_id: str = ""
    apple_client_ids: tuple[str, ...] = ()
    apple_jwks_url: str = DEFAULT_APPLE_JWKS_URL
    apple_root_fingerprints: tuple[str, ...] = ()
    appstore_allow_xcode: bool = False
    appstore_products: Mapping[str, str] = field(default_factory=dict)
    plan_minutes: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_PLAN_MINUTES))
    worker_concurrency: int = 1
    worker_poll_seconds: float = 2.0
    embedded_worker: bool = False
    sse_poll_seconds: float = 1.0
    sse_keepalive_seconds: float = 10.0
    default_ui_language: str = "en"

    # -- Derived values ----------------------------------------------------

    @property
    def public_base(self) -> str:
        """Configured public origin without a trailing slash."""
        return self.base_url.rstrip("/")

    @property
    def base_host(self) -> str:
        """``host[:port]`` of the public origin, lowercased."""
        return urlsplit(self.public_base).netloc.lower()

    @property
    def secure_cookies(self) -> bool:
        """Cookies carry ``Secure`` when forced or when the public origin is HTTPS."""
        return self.cookie_secure or self.public_base.lower().startswith("https://")

    @property
    def database_file(self) -> Path:
        return self.database_path or (self.storage_dir / "cloud.sqlite3")

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def reviews_dir(self) -> Path:
        return self.storage_dir / "reviews"

    @property
    def glossary_root(self) -> Path:
        return self.storage_dir / "glossary"

    @property
    def has_ai_capacity(self) -> bool:
        """True when a server-owned AI key is configured (never a customer key)."""
        return bool(self.libraxis_api_key)

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def apple_audiences(self) -> tuple[str, ...]:
        """Accepted ``aud`` values for Sign in with Apple identity tokens."""
        if self.apple_client_ids:
            return self.apple_client_ids
        return (self.apple_bundle_id,) if self.apple_bundle_id else ()

    def url(self, path: str) -> str:
        """Absolute public URL for a server-relative ``path``."""
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{self.public_base}{suffix}"

    def allowed_origin_hosts(self) -> frozenset[str]:
        """Hosts accepted in ``Origin``/``Referer`` for cookie-authenticated writes."""
        hosts = {self.base_host}
        hosts.update(host.lower() for host in self.trusted_hosts)
        for origin in self.cors_origins:
            netloc = urlsplit(origin).netloc.lower()
            if netloc:
                hosts.add(netloc)
        return frozenset(host for host in hosts if host)

    def minutes_for_plan(self, plan: str) -> float:
        return float(self.plan_minutes.get(plan, 0.0))

    def plan_for_price(self, price_id: str) -> str | None:
        for plan, configured in self.stripe_prices.items():
            if configured == price_id:
                return plan
        return None

    # -- Construction ------------------------------------------------------

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CloudSettings:
        """Build settings from ``environ`` (defaults to ``os.environ``)."""
        env: Mapping[str, str] = os.environ if environ is None else environ
        storage = Path(env.get("SCREENSCRIBE_CLOUD_STORAGE_DIR", "").strip() or DEFAULT_STORAGE_DIR)
        database = _optional(env, "SCREENSCRIBE_CLOUD_DATABASE")
        plan_minutes: dict[str, float] = dict(DEFAULT_PLAN_MINUTES)
        for plan, raw in _pairs(env.get("SCREENSCRIBE_CLOUD_PLAN_MINUTES")).items():
            plan_minutes[plan] = _float(raw, plan_minutes.get(plan, 0.0))
        stripe_prices = {
            plan: price
            for plan, price in (
                ("starter", env.get("SCREENSCRIBE_CLOUD_STRIPE_PRICE_STARTER", "").strip()),
                ("pro", env.get("SCREENSCRIBE_CLOUD_STRIPE_PRICE_PRO", "").strip()),
                ("team", env.get("SCREENSCRIBE_CLOUD_STRIPE_PRICE_TEAM", "").strip()),
            )
            if price
        }
        return cls(
            base_url=env.get("SCREENSCRIBE_CLOUD_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            host=env.get("SCREENSCRIBE_CLOUD_HOST", "").strip() or DEFAULT_HOST,
            port=_int(env.get("SCREENSCRIBE_CLOUD_PORT"), DEFAULT_PORT, minimum=1),
            storage_dir=storage,
            database_path=Path(database) if database else None,
            libraxis_api_key=_first(env, "SCREENSCRIBE_API_KEY", "LIBRAXIS_API_KEY"),
            api_base=_optional(env, "SCREENSCRIBE_CLOUD_API_BASE"),
            stt_endpoint=_optional(env, "SCREENSCRIBE_CLOUD_STT_ENDPOINT"),
            llm_endpoint=_optional(env, "SCREENSCRIBE_CLOUD_LLM_ENDPOINT"),
            vision_endpoint=_optional(env, "SCREENSCRIBE_CLOUD_VISION_ENDPOINT"),
            llm_model=_optional(env, "SCREENSCRIBE_CLOUD_LLM_MODEL"),
            vision_model=_optional(env, "SCREENSCRIBE_CLOUD_VISION_MODEL"),
            max_upload_bytes=_int(
                env.get("SCREENSCRIBE_CLOUD_MAX_UPLOAD_BYTES"), DEFAULT_MAX_UPLOAD_BYTES, minimum=1
            ),
            max_bundle_bytes=_int(
                env.get("SCREENSCRIBE_CLOUD_MAX_BUNDLE_BYTES"), DEFAULT_MAX_BUNDLE_BYTES, minimum=1
            ),
            max_archive_members=_int(
                env.get("SCREENSCRIBE_CLOUD_MAX_ARCHIVE_MEMBERS"),
                DEFAULT_MAX_ARCHIVE_MEMBERS,
                minimum=1,
            ),
            max_archive_total_bytes=_int(
                env.get("SCREENSCRIBE_CLOUD_MAX_ARCHIVE_TOTAL_BYTES"),
                DEFAULT_MAX_ARCHIVE_TOTAL_BYTES,
                minimum=1,
            ),
            min_job_minutes=_float(env.get("SCREENSCRIBE_CLOUD_MIN_JOB_MINUTES"), 0.25),
            refresh_ttl_seconds=_int(
                env.get("SCREENSCRIBE_CLOUD_REFRESH_TTL_SECONDS"),
                DEFAULT_REFRESH_TTL_SECONDS,
                minimum=60,
            ),
            web_session_ttl_seconds=_int(
                env.get("SCREENSCRIBE_CLOUD_WEB_SESSION_TTL_SECONDS"),
                DEFAULT_WEB_SESSION_TTL_SECONDS,
                minimum=60,
            ),
            device_code_ttl_seconds=_int(
                env.get("SCREENSCRIBE_CLOUD_DEVICE_CODE_TTL_SECONDS"),
                DEFAULT_DEVICE_CODE_TTL_SECONDS,
                minimum=30,
            ),
            device_poll_interval=_int(
                env.get("SCREENSCRIBE_CLOUD_DEVICE_POLL_INTERVAL"), DEFAULT_DEVICE_POLL_INTERVAL
            ),
            cookie_secure=_flag(env.get("SCREENSCRIBE_CLOUD_COOKIE_SECURE")),
            cors_origins=_csv(env.get("SCREENSCRIBE_CLOUD_CORS_ORIGINS")),
            trusted_hosts=_csv(env.get("SCREENSCRIBE_CLOUD_TRUSTED_HOSTS")),
            stripe_secret_key=_first(
                env, "SCREENSCRIBE_CLOUD_STRIPE_SECRET_KEY", "STRIPE_SECRET_KEY"
            ),
            stripe_webhook_secret=_first(
                env, "SCREENSCRIBE_CLOUD_STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET"
            ),
            stripe_api_base=env.get("SCREENSCRIBE_CLOUD_STRIPE_API_BASE", "").strip()
            or DEFAULT_STRIPE_API_BASE,
            stripe_prices=stripe_prices,
            apple_bundle_id=env.get("SCREENSCRIBE_CLOUD_APPLE_BUNDLE_ID", "").strip(),
            apple_client_ids=_csv(env.get("SCREENSCRIBE_CLOUD_APPLE_CLIENT_IDS")),
            apple_jwks_url=env.get("SCREENSCRIBE_CLOUD_APPLE_JWKS_URL", "").strip()
            or DEFAULT_APPLE_JWKS_URL,
            apple_root_fingerprints=_csv(env.get("SCREENSCRIBE_CLOUD_APPLE_ROOT_SHA256")),
            appstore_allow_xcode=_flag(env.get("SCREENSCRIBE_CLOUD_APPSTORE_ALLOW_XCODE")),
            appstore_products=_pairs(env.get("SCREENSCRIBE_CLOUD_APPSTORE_PRODUCTS")),
            plan_minutes=plan_minutes,
            worker_concurrency=_int(env.get("SCREENSCRIBE_CLOUD_WORKER_CONCURRENCY"), 1, minimum=1),
            worker_poll_seconds=_float(
                env.get("SCREENSCRIBE_CLOUD_WORKER_POLL_SECONDS"), 2.0, minimum=0.05
            ),
            embedded_worker=_flag(env.get("SCREENSCRIBE_CLOUD_EMBEDDED_WORKER")),
            sse_poll_seconds=_float(
                env.get("SCREENSCRIBE_CLOUD_SSE_POLL_SECONDS"), 1.0, minimum=0.05
            ),
            sse_keepalive_seconds=_float(
                env.get("SCREENSCRIBE_CLOUD_SSE_KEEPALIVE_SECONDS"), 10.0, minimum=0.5
            ),
            default_ui_language=(
                "pl"
                if env.get("SCREENSCRIBE_CLOUD_DEFAULT_LANG", "").strip().lower() == "pl"
                else "en"
            ),
        )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PLAN_MINUTES",
    "DEFAULT_PORT",
    "CloudSettings",
]
