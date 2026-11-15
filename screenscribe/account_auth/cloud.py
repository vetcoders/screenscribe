"""Screenscribe Cloud account: RFC 8628 device sign-in, refresh and quota.

``screenscribe auth login cloud`` signs the CLI in to the hosted Screenscribe
subscription. The session shares the provider-account store --
``~/.config/screenscribe/accounts.json`` (mode 0600, parent 0700, every
read-modify-write under the ``accounts.json.lock`` exclusive lock) -- in its
own ``accounts["cloud"]`` slot, so the ``openai`` / ``xai`` entries are never
touched.

Wire contract (JSON bodies, ``client_id = "screenscribe-cli"``):

- ``POST {cloud}/api/v1/auth/device/code``   -> device code + user code
- ``POST {cloud}/api/v1/auth/device/token``  -> RFC 8628 poll
- ``POST {cloud}/api/v1/auth/token/refresh`` -> rotated bearer pair
- ``GET  {cloud}/api/v1/auth/me``            -> identity + subscription quota

Host resolution: ``--cloud-url`` -> ``SCREENSCRIBE_CLOUD_URL`` -> the stored
session's ``cloud_url`` -> :data:`DEFAULT_CLOUD_URL`. Remote hosts must be
HTTPS; plain HTTP is accepted only on a literal loopback address (local
integration servers), through ``api_utils.validate_provider_endpoint`` and
``api_utils.provider_client_options`` (no proxies on loopback, no redirects).

A stored session is bound to the host that issued it: its bearer is never sent
to another host and its refresh token only ever goes back to the issuer.
Nothing in this module prints, logs or raises a token.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..api_utils import (
    UnsafeEndpointError,
    endpoint_host,
    provider_client_options,
    redact_urls_in_text,
    validate_provider_endpoint,
)
from . import (
    BEARER_TYPE,
    AccountAuthError,
    AccountTokens,
    _now,
    _optional_int,
    _optional_str,
    _read_store,
    _store_lock,
    _store_writable,
    _token_type,
    _write_store,
    accounts_path,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Slot name in ``accounts.json`` and the ``screenscribe auth`` provider name.
CLOUD_PROVIDER = "cloud"
#: Public client id of the Screenscribe CLI on the Cloud device flow.
CLOUD_CLIENT_ID = "screenscribe-cli"
#: Environment override for the Cloud base URL.
CLOUD_URL_ENV = "SCREENSCRIBE_CLOUD_URL"
#: Production Screenscribe Cloud.
DEFAULT_CLOUD_URL = "https://app.screenscribe.vetcoders.io"

CLOUD_LOGIN_HINT = "Run: screenscribe auth login cloud"
NOT_SIGNED_IN_MESSAGE = f"Not signed in to Screenscribe Cloud. {CLOUD_LOGIN_HINT}"

DEVICE_CODE_PATH = "/api/v1/auth/device/code"
DEVICE_POLL_PATH = "/api/v1/auth/device/token"
REFRESH_PATH = "/api/v1/auth/token/refresh"
IDENTITY_PATH = "/api/v1/auth/me"

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"

#: Auth calls are small; the refresh runs under the store lock, so connect +
#: write + read stays well below ``STORE_LOCK_TIMEOUT_SECONDS`` (45 s).
CLOUD_AUTH_TIMEOUT = httpx.Timeout(10.0, read=20.0)

#: Poll interval when the device-code response omits one (RFC 8628 default).
DEFAULT_INTERVAL_SECONDS = 5
#: Added to the poll interval on every ``slow_down`` answer (RFC 8628 3.5).
SLOW_DOWN_INCREMENT_SECONDS = 5
#: Upper bound for any server-advertised poll interval.
MAX_INTERVAL_SECONDS = 60
#: Whole sign-in budget (the device code usually expires first).
DEFAULT_MAX_WAIT_SECONDS = 15 * 60
#: Consecutive transport failures tolerated while polling.
MAX_POLL_TRANSPORT_FAILURES = 3

_PENDING = "authorization_pending"
_SLOW_DOWN = "slow_down"
_DENIED = "access_denied"
_EXPIRED = "expired_token"

_JSON_HEADERS = {"Accept": "application/json"}
_SAFE_CODE = re.compile(r"[^a-z0-9_.\-]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_URL_MESSAGE = (
    "Screenscribe Cloud URL must be https:// (plain http:// is accepted only on "
    "localhost or a literal loopback address)."
)
_UNREACHABLE_NOTE = "cached — server unreachable"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CloudAuthError(AccountAuthError):
    """Cloud session failure. ``kind`` follows :class:`AccountAuthError`.

    Extra kinds: ``config`` (unusable Cloud URL or a redirecting endpoint) and
    ``unauthorized`` (the Cloud API rejected the bearer with HTTP 401; the
    caller refreshes once and replays).
    """


class CloudAuthRequiredError(CloudAuthError):
    """No usable Cloud session for the target host: only a new sign-in helps."""

    def __init__(self, message: str = NOT_SIGNED_IN_MESSAGE) -> None:
        super().__init__("not_signed_in", message)


# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #


def clean_server_text(value: object, limit: int = 300) -> str | None:
    """Server text safe for a terminal: no URL secrets, no control chars."""
    if not isinstance(value, str):
        return None
    text = _CONTROL_CHARS.sub(" ", redact_urls_in_text(value))
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def safe_browser_url(value: object) -> str | None:
    """An http(s) URL the CLI may print or open; never another scheme."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        validate_provider_endpoint(candidate)
        parts = httpx.URL(candidate)
    except (UnsafeEndpointError, httpx.InvalidURL):
        return None
    if parts.userinfo or any(char in candidate for char in "[]"):
        return None
    return candidate


def _fmt_minutes(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class CloudSubscription:
    """``subscription`` object of ``GET /api/v1/auth/me``."""

    active: bool
    plan: str | None = None
    source: str | None = None
    period_end: str | None = None
    minutes_limit: float | None = None
    minutes_used: float | None = None
    minutes_remaining: float | None = None

    @classmethod
    def from_payload(cls, value: object) -> CloudSubscription | None:
        if not isinstance(value, dict):
            return None
        return cls(
            active=value.get("active") is True,
            plan=clean_server_text(value.get("plan"), 60),
            source=clean_server_text(value.get("source"), 60),
            period_end=clean_server_text(value.get("period_end"), 60),
            minutes_limit=_number(value.get("minutes_limit")),
            minutes_used=_number(value.get("minutes_used")),
            minutes_remaining=_number(value.get("minutes_remaining")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def plan_line(self) -> str:
        """``plan: pro (active, stripe)``."""
        state = "active" if self.active else "inactive"
        source = f", {self.source}" if self.source else ""
        return f"plan: {self.plan or 'unknown'} ({state}{source})"

    def quota_line(self) -> str | None:
        """``minutes: 15 / 120 (105 remaining)`` when the quota is known."""
        if self.minutes_used is None and self.minutes_limit is None:
            if self.minutes_remaining is None:
                return None
            return f"minutes: {_fmt_minutes(self.minutes_remaining)} remaining"
        used = _fmt_minutes(self.minutes_used) if self.minutes_used is not None else "?"
        limit = _fmt_minutes(self.minutes_limit) if self.minutes_limit is not None else "?"
        line = f"minutes: {used} / {limit}"
        if self.minutes_remaining is not None:
            line += f" ({_fmt_minutes(self.minutes_remaining)} remaining)"
        return line

    def summary_lines(self) -> list[str]:
        """Plan, quota and period, one fact per line."""
        lines = [self.plan_line()]
        quota = self.quota_line()
        if quota:
            lines.append(quota)
        if self.period_end:
            lines.append(f"period ends: {self.period_end}")
        return lines

    def describe(self) -> str:
        """One-line summary for ``auth login``."""
        return "; ".join(self.summary_lines())


@dataclass(frozen=True)
class CloudIdentity:
    """``GET /api/v1/auth/me``: who the bearer belongs to and their quota."""

    user_id: str | None
    email: str | None
    subscription: CloudSubscription | None

    @classmethod
    def from_payload(cls, body: dict[str, Any]) -> CloudIdentity:
        return cls(
            user_id=clean_server_text(body.get("user_id"), 120),
            email=clean_server_text(body.get("email"), 254),
            subscription=CloudSubscription.from_payload(body.get("subscription")),
        )


@dataclass
class CloudSession:
    """The stored ``accounts["cloud"]`` entry: tokens bound to one Cloud host."""

    tokens: AccountTokens
    cloud_url: str
    user_id: str | None = None
    email: str | None = None
    subscription: CloudSubscription | None = None

    @property
    def host(self) -> str:
        return endpoint_host(self.cloud_url)

    def authorization(self) -> dict[str, str]:
        """``Authorization`` header for this host only."""
        return {"Authorization": f"{BEARER_TYPE} {self.tokens.access_token}"}

    def needs_refresh(self) -> bool:
        """Refresh proactively: expiry unknown or within the 60 s skew."""
        if not self.tokens.refresh_token:
            return False
        return self.tokens.expires_at is None or self.tokens.expires_within()

    def with_identity(self, identity: CloudIdentity) -> CloudSession:
        return replace(
            self,
            user_id=identity.user_id or self.user_id,
            email=identity.email or self.email,
            subscription=identity.subscription or self.subscription,
        )

    def __repr__(self) -> str:  # never leak a token through repr/logging
        return f"CloudSession(host={self.host!r}, email={self.email!r}, tokens={self.tokens!r})"


@dataclass(frozen=True)
class CloudDeviceCode:
    """What to show the user and what to poll with."""

    cloud_url: str
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int

    @property
    def verification_url(self) -> str:
        return self.verification_uri_complete or self.verification_uri

    @property
    def host(self) -> str:
        return endpoint_host(self.cloud_url)

    def __repr__(self) -> str:  # the device code is a poll secret
        return (
            f"CloudDeviceCode(host={self.host!r}, user_code={self.user_code!r}, "
            f"interval={self.interval!r}, expires_in={self.expires_in!r})"
        )


@dataclass(frozen=True)
class SubscriptionRequired:
    """HTTP 402 ``subscription_required`` body (also used for async job failures)."""

    code: str
    message: str
    upgrade_url: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "error": "subscription_required",
            "code": self.code,
            "message": self.message,
            "upgrade_url": self.upgrade_url,
        }


def parse_subscription_required(payload: object) -> SubscriptionRequired:
    """Read a 402 body; tolerates FastAPI's ``{"detail": {...}}`` envelope."""
    body: dict[str, Any] = payload if isinstance(payload, dict) else {}
    detail = body.get("detail")
    if "message" not in body and isinstance(detail, dict):
        body = detail
    raw_code = body.get("code")
    code = _SAFE_CODE.sub("", raw_code.lower())[:64] if isinstance(raw_code, str) else ""
    message = clean_server_text(body.get("message")) or (
        clean_server_text(detail) if isinstance(detail, str) else None
    )
    return SubscriptionRequired(
        code=code or "subscription_required",
        message=message or "An active Screenscribe subscription is required.",
        upgrade_url=safe_browser_url(body.get("upgrade_url")),
    )


# --------------------------------------------------------------------------- #
# Cloud URL + transport
# --------------------------------------------------------------------------- #


def normalize_cloud_url(url: str) -> str:
    """``scheme://host[:port][/base]`` without trailing slash, query or fragment.

    Raises :class:`CloudAuthError` (``config``) for anything but HTTPS, or
    plain HTTP on a literal loopback address, and for URLs with credentials.
    """
    raw = url.strip()
    try:
        validate_provider_endpoint(raw)
        parsed = httpx.URL(raw)
        path = urlsplit(raw).path
    except (UnsafeEndpointError, httpx.InvalidURL, ValueError):
        raise CloudAuthError("config", _UNSAFE_URL_MESSAGE) from None
    if parsed.userinfo:
        raise CloudAuthError("config", "Screenscribe Cloud URL must not embed credentials.")
    host = parsed.host.lower()
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return f"{parsed.scheme}://{netloc}{path.rstrip('/')}"


def resolve_cloud_url(flag: str | None = None) -> str:
    """``--cloud-url`` -> ``SCREENSCRIBE_CLOUD_URL`` -> stored host -> default."""
    for candidate in (flag, os.environ.get(CLOUD_URL_ENV)):
        if candidate and candidate.strip():
            return normalize_cloud_url(candidate)
    session = load_cloud_session()
    if session is not None:
        return session.cloud_url
    return DEFAULT_CLOUD_URL


def _build_http_client(
    cloud_url: str, *, timeout: httpx.Timeout = CLOUD_AUTH_TIMEOUT
) -> httpx.Client:
    """The one factory for Cloud HTTP clients (auth here, uploads in ``cli_publish``).

    TLS for remote hosts; loopback HTTP goes direct (no proxy env, no proxy
    mounts). Redirects are never followed, so a checked destination cannot be
    swapped for another host. ``timeout`` is always explicit.
    """
    try:
        options = provider_client_options(cloud_url)
    except UnsafeEndpointError:
        raise CloudAuthError("config", _UNSAFE_URL_MESSAGE) from None
    return httpx.Client(timeout=timeout, **options)


@contextlib.contextmanager
def _client_for(cloud_url: str, client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    own = _build_http_client(cloud_url)
    try:
        yield own
    finally:
        own.close()


def _request(
    client: httpx.Client, method: str, url: str, *, what: str, **kwargs: Any
) -> httpx.Response:
    """One auth request: endpoint re-checked at the transport boundary, no redirects."""
    try:
        validate_provider_endpoint(url)
    except UnsafeEndpointError:
        raise CloudAuthError("config", _UNSAFE_URL_MESSAGE) from None
    try:
        response = client.request(
            method, url, timeout=CLOUD_AUTH_TIMEOUT, follow_redirects=False, **kwargs
        )
    except httpx.HTTPError as error:
        raise CloudAuthError("http", f"{what} failed: {error.__class__.__name__}") from None
    if 300 <= response.status_code < 400:
        raise CloudAuthError(
            "config",
            f"{what} answered with a redirect (status {response.status_code}); "
            "refusing to follow it. Check --cloud-url / SCREENSCRIBE_CLOUD_URL.",
        )
    return response


def _json_body(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise CloudAuthError(
            "http", f"{what} returned a non-JSON body (status {response.status_code})"
        ) from None
    if not isinstance(body, dict):
        raise CloudAuthError("http", f"{what} returned an unexpected JSON body")
    return body


def oauth_error_code(response: httpx.Response) -> str:
    """Sanitized RFC 6749 ``error`` code of an error body, ``""`` when absent."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        error = error.get("code") or error.get("type")
    if not isinstance(error, str):
        detail = body.get("detail")  # FastAPI HTTPException envelope
        error = detail.get("error") if isinstance(detail, dict) else detail
    if not isinstance(error, str):
        return ""
    return _SAFE_CODE.sub("", error.lower())[:64]


def _is_transient(status: int) -> bool:
    return status in (408, 429) or status >= 500


# --------------------------------------------------------------------------- #
# Store (accounts["cloud"])
# --------------------------------------------------------------------------- #


def _session_entry(session: CloudSession) -> dict[str, Any]:
    entry: dict[str, Any] = asdict(session.tokens)
    entry["cloud_url"] = session.cloud_url
    entry["user_id"] = session.user_id
    entry["email"] = session.email
    entry["subscription"] = session.subscription.to_dict() if session.subscription else None
    return entry


def _session_from_entry(entry: object) -> CloudSession | None:
    if not isinstance(entry, dict):
        return None
    access = entry.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    raw_url = entry.get("cloud_url")
    try:
        cloud_url = (
            normalize_cloud_url(raw_url)
            if isinstance(raw_url, str) and raw_url.strip()
            else DEFAULT_CLOUD_URL
        )
    except CloudAuthError:
        return None
    tokens = AccountTokens(
        access_token=access,
        refresh_token=_optional_str(entry.get("refresh_token")),
        token_type=_token_type(entry.get("token_type")),
        expires_at=_optional_int(entry.get("expires_at")),
        authorized_at=_optional_int(entry.get("authorized_at")),
        disabled_cause=_optional_str(entry.get("disabled_cause")),
    )
    return CloudSession(
        tokens=tokens,
        cloud_url=cloud_url,
        user_id=_optional_str(entry.get("user_id")),
        email=_optional_str(entry.get("email")),
        subscription=CloudSubscription.from_payload(entry.get("subscription")),
    )


def load_cloud_session() -> CloudSession | None:
    """The stored Cloud session, or ``None`` when signed out. No network.

    Readers do not lock: writes land by atomic rename (same as ``account_auth``).
    """
    return _session_from_entry(_read_store()["accounts"].get(CLOUD_PROVIDER))


def _put_session(session: CloudSession) -> None:
    """Write the cloud slot. Caller holds :func:`_store_lock`."""
    store = _read_store()
    store["accounts"][CLOUD_PROVIDER] = _session_entry(session)
    _write_store(store)


def store_cloud_session(session: CloudSession) -> None:
    """Persist ``session`` (0600); other providers' slots survive."""
    with _store_lock():
        _put_session(session)


def clear_cloud_session() -> bool:
    """Sign out of Cloud. Returns True when an entry was removed."""
    with _store_lock():
        store = _read_store()
        removed = store["accounts"].pop(CLOUD_PROVIDER, None) is not None
        if removed:
            _write_store(store)
    return bool(removed)


# --------------------------------------------------------------------------- #
# Device flow
# --------------------------------------------------------------------------- #


def _interval(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return DEFAULT_INTERVAL_SECONDS
    return min(int(value), MAX_INTERVAL_SECONDS)


def _expires_in(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return DEFAULT_MAX_WAIT_SECONDS
    return int(value)


def request_cloud_device_code(
    cloud_url: str | None = None, *, client: httpx.Client | None = None
) -> CloudDeviceCode:
    """Start the device flow on the resolved Cloud host."""
    target = resolve_cloud_url(cloud_url)
    url = f"{target}{DEVICE_CODE_PATH}"
    with _client_for(target, client) as http:
        response = _request(
            http,
            "POST",
            url,
            what="Cloud sign-in start",
            json={"client_id": CLOUD_CLIENT_ID},
            headers=_JSON_HEADERS,
        )
    status = response.status_code
    if status >= 400:
        reason = oauth_error_code(response) or f"status {status}"
        kind = "http" if _is_transient(status) else "oauth"
        raise CloudAuthError(kind, f"Cloud sign-in start refused ({reason})")
    body = _json_body(response, "Cloud sign-in start")
    device_code = body.get("device_code")
    user_code = clean_server_text(body.get("user_code"), 32)
    verification_uri = safe_browser_url(body.get("verification_uri"))
    if not isinstance(device_code, str) or not device_code or not user_code:
        raise CloudAuthError("oauth", "Cloud sign-in start response is incomplete")
    if verification_uri is None:
        raise CloudAuthError("oauth", "Cloud sign-in start returned no usable verification URL")
    return CloudDeviceCode(
        cloud_url=target,
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=safe_browser_url(body.get("verification_uri_complete")),
        expires_in=_expires_in(body.get("expires_in")),
        interval=_interval(body.get("interval")),
    )


def poll_cloud_device_token(
    device: CloudDeviceCode,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> AccountTokens:
    """Poll until the user approves (RFC 8628 3.4/3.5). Nothing is stored here."""
    budget = min(device.expires_in, max_wait_seconds)
    deadline = monotonic() + budget
    interval = device.interval
    failures = 0
    url = f"{device.cloud_url}{DEVICE_POLL_PATH}"
    payload = {
        "grant_type": DEVICE_CODE_GRANT,
        "device_code": device.device_code,
        "client_id": CLOUD_CLIENT_ID,
    }
    with _client_for(device.cloud_url, client) as http:
        while True:
            if monotonic() >= deadline:
                raise CloudAuthError(
                    "oauth", f"Timed out waiting for the browser approval. {CLOUD_LOGIN_HINT}"
                )
            sleep(interval)
            try:
                response = _request(
                    http,
                    "POST",
                    url,
                    what="Cloud sign-in poll",
                    json=payload,
                    headers=_JSON_HEADERS,
                )
            except CloudAuthError as error:
                failures += 1
                if error.kind != "http" or failures >= MAX_POLL_TRANSPORT_FAILURES:
                    raise
                continue
            status = response.status_code
            if status < 300:
                tokens = AccountTokens.from_token_response(_json_body(response, "Cloud sign-in"))
                tokens.authorized_at = _now()
                return tokens
            code = oauth_error_code(response)
            if code == _PENDING:
                failures = 0
                continue
            if code == _SLOW_DOWN:
                failures = 0
                interval = min(interval + SLOW_DOWN_INCREMENT_SECONDS, MAX_INTERVAL_SECONDS)
                continue
            if code == _DENIED:
                raise CloudAuthError(
                    "oauth", "Sign-in was denied in the browser. Nothing was stored."
                )
            if code == _EXPIRED:
                raise CloudAuthError(
                    "oauth", f"The sign-in code expired before approval. {CLOUD_LOGIN_HINT}"
                )
            if _is_transient(status):
                failures += 1
                if failures >= MAX_POLL_TRANSPORT_FAILURES:
                    raise CloudAuthError("http", f"Cloud sign-in poll returned status {status}")
                continue
            raise CloudAuthError("oauth", f"Cloud sign-in refused ({code or f'status {status}'})")


def fetch_cloud_identity(
    cloud_url: str, access_token: str, *, client: httpx.Client | None = None
) -> CloudIdentity:
    """``GET /api/v1/auth/me``. HTTP 401 raises ``CloudAuthError("unauthorized")``."""
    url = f"{cloud_url}{IDENTITY_PATH}"
    headers = {**_JSON_HEADERS, "Authorization": f"{BEARER_TYPE} {access_token}"}
    with _client_for(cloud_url, client) as http:
        response = _request(http, "GET", url, what="Cloud account lookup", headers=headers)
    status = response.status_code
    if status == 401:
        raise CloudAuthError("unauthorized", "Screenscribe Cloud rejected the stored session")
    if status >= 400:
        kind = "http" if _is_transient(status) else "oauth"
        raise CloudAuthError(kind, f"Cloud account lookup returned status {status}")
    return CloudIdentity.from_payload(_json_body(response, "Cloud account lookup"))


def complete_cloud_login(
    device: CloudDeviceCode,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> CloudSession:
    """Poll, look up identity + quota, persist ``accounts["cloud"]`` (0600).

    A failing ``/api/v1/auth/me`` does not undo the sign-in: the session is
    stored and ``auth status`` retries the lookup later.
    """
    tokens = poll_cloud_device_token(device, client=client, sleep=sleep, monotonic=monotonic)
    session = CloudSession(tokens=tokens, cloud_url=device.cloud_url)
    try:
        identity = fetch_cloud_identity(device.cloud_url, tokens.access_token, client=client)
    except CloudAuthError:
        identity = None
    if identity is not None:
        session = session.with_identity(identity)
    store_cloud_session(session)
    return session


# --------------------------------------------------------------------------- #
# Refresh + bearer resolution
# --------------------------------------------------------------------------- #


def _ended_message(cause: str) -> str:
    return f"Screenscribe Cloud sign-in ended ({cause}). {CLOUD_LOGIN_HINT}"


def _require_host(session: CloudSession, cloud_url: str) -> None:
    """The bearer stays on the host that issued it."""
    if session.cloud_url != cloud_url:
        raise CloudAuthRequiredError(
            f"The stored Screenscribe Cloud session belongs to {session.host}, not "
            f"{endpoint_host(cloud_url)}. Run: screenscribe auth login cloud "
            f"--cloud-url {cloud_url}"
        )


def _request_cloud_refresh(session: CloudSession, http: httpx.Client) -> AccountTokens:
    """One ``grant_type=refresh_token`` call to the issuing host."""
    refresh = session.tokens.refresh_token
    if not refresh:
        raise CloudAuthRequiredError(
            f"The stored Screenscribe Cloud session cannot be renewed. {CLOUD_LOGIN_HINT}"
        )
    body = {"grant_type": REFRESH_GRANT, "refresh_token": refresh, "client_id": CLOUD_CLIENT_ID}
    response = _request(
        http,
        "POST",
        f"{session.cloud_url}{REFRESH_PATH}",
        what="Cloud session refresh",
        json=body,
        headers=_JSON_HEADERS,
    )
    status = response.status_code
    if status >= 400:
        if _is_transient(status):
            raise CloudAuthError("http", f"Cloud session refresh returned status {status}")
        reason = oauth_error_code(response) or f"status {status}"
        raise CloudAuthError("revoked", f"refresh refused: {reason}")
    refreshed = AccountTokens.from_token_response(_json_body(response, "Cloud session refresh"))
    if refreshed.refresh_token is None:
        refreshed.refresh_token = refresh
    refreshed.authorized_at = session.tokens.authorized_at
    return refreshed


def _refresh_locked(
    seen_access_token: str, cloud_url: str, client: httpx.Client | None, *, force: bool
) -> CloudSession:
    """Refresh under the store lock, reusing a peer process's rotation.

    Mirrors ``account_auth._refresh_locked``: when the stored bearer differs
    from the one the caller holds, another process already rotated the pair
    and its result is returned without a network call. A definitive refusal
    marks the session as ended (``auth status`` shows it) and raises
    :class:`CloudAuthRequiredError`.
    """
    with _store_lock():
        current = load_cloud_session()
        if current is None:
            raise CloudAuthRequiredError()
        _require_host(current, cloud_url)
        if current.tokens.disabled_cause:
            raise CloudAuthRequiredError(_ended_message(current.tokens.disabled_cause))
        peer_rotated = current.tokens.access_token != seen_access_token
        if peer_rotated and (force or not current.needs_refresh()):
            return current
        if not _store_writable():
            raise CloudAuthError("storage", f"cannot write {accounts_path()}: read-only")
        with _client_for(current.cloud_url, client) as http:
            try:
                tokens = _request_cloud_refresh(current, http)
            except CloudAuthError as error:
                if error.kind != "revoked":
                    raise
                current.tokens.disabled_cause = str(error)
                _put_session(current)
                raise CloudAuthRequiredError(_ended_message(str(error))) from None
        refreshed = replace(current, tokens=tokens)
        _put_session(refreshed)
        return refreshed


def resolve_cloud_bearer(
    cloud_url: str | None = None, *, client: httpx.Client | None = None
) -> CloudSession:
    """A usable session for the resolved host, refreshed proactively.

    Raises :class:`CloudAuthRequiredError` when signed out, when the stored
    session belongs to another host, or when it can no longer be renewed.
    """
    target = resolve_cloud_url(cloud_url)
    session = load_cloud_session()
    if session is None:
        raise CloudAuthRequiredError()
    _require_host(session, target)
    if session.tokens.disabled_cause:
        raise CloudAuthRequiredError(_ended_message(session.tokens.disabled_cause))
    if not session.tokens.refresh_token and not session.tokens.still_valid():
        raise CloudAuthRequiredError(
            f"The stored Screenscribe Cloud session expired. {CLOUD_LOGIN_HINT}"
        )
    if session.needs_refresh():
        return _refresh_locked(session.tokens.access_token, target, client, force=False)
    return session


def force_cloud_refresh(
    rejected_access_token: str,
    cloud_url: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> CloudSession:
    """Refresh once after the Cloud API answered 401 to ``rejected_access_token``."""
    target = resolve_cloud_url(cloud_url)
    return _refresh_locked(rejected_access_token, target, client, force=True)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CloudStatus:
    """``auth status`` view of the Cloud session."""

    session: CloudSession
    live: bool
    note: str | None = None
    needs_login: bool = False


def _remember_identity(session: CloudSession, identity: CloudIdentity) -> CloudSession:
    """Cache the fresh ``/me`` answer, unless another process replaced the session.

    The cache is best effort: a read-only or busy store still shows live data.
    """
    updated = session.with_identity(identity)
    try:
        with _store_lock():
            current = load_cloud_session()
            if current is not None and current.tokens.access_token == session.tokens.access_token:
                _put_session(updated)
    except AccountAuthError:
        pass
    return updated


def cloud_status(*, client: httpx.Client | None = None) -> CloudStatus | None:
    """``None`` when signed out (zero network calls); else a live ``/me`` lookup.

    The lookup refreshes the bearer when needed and replays once after a 401.
    When the server is unreachable the cached identity and quota are returned
    with ``live=False``.
    """
    stored = load_cloud_session()
    if stored is None:
        return None
    if stored.tokens.disabled_cause:
        note = f"sign-in ended ({stored.tokens.disabled_cause})"
        return CloudStatus(stored, live=False, note=note, needs_login=True)
    try:
        session = resolve_cloud_bearer(stored.cloud_url, client=client)
        try:
            identity = fetch_cloud_identity(
                session.cloud_url, session.tokens.access_token, client=client
            )
        except CloudAuthError as error:
            if error.kind != "unauthorized":
                raise
            session = force_cloud_refresh(
                session.tokens.access_token, session.cloud_url, client=client
            )
            identity = fetch_cloud_identity(
                session.cloud_url, session.tokens.access_token, client=client
            )
    except CloudAuthRequiredError as error:
        return CloudStatus(stored, live=False, note=str(error), needs_login=True)
    except CloudAuthError as error:
        if error.kind == "unauthorized":
            return CloudStatus(
                stored, live=False, note="the server rejected the session", needs_login=True
            )
        note = _UNREACHABLE_NOTE if error.kind == "http" else str(error)
        return CloudStatus(stored, live=False, note=note)
    return CloudStatus(_remember_identity(session, identity), live=True)
