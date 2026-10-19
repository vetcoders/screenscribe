"""Provider-account (OAuth) authentication for Screenscribe.

Port of Codescribe ``core/llm/account_auth`` (Rust, read-only reference) to a
file-backed Python store. Two providers can sign in with an account:

- ``openai`` -- Codex device flow (``{issuer}/api/accounts/deviceauth/*`` then a
  PKCE code exchange on ``{issuer}/oauth/token``). The ChatGPT account token is
  a bearer only on the ChatGPT Codex backend (``chatgpt.com``, LLM + vision --
  see :mod:`screenscribe.providers.chatgpt`). ``api.openai.com`` rejects it
  (Codescribe, 2026-09-08), so it never goes there -- see :func:`resolve_bearer`.
- ``xai`` -- RFC 8628 device authorization on ``https://auth.x.ai``. The token
  carries ``api:access`` and is usable on ``api.x.ai``.

Tokens live in ``~/.config/screenscribe/accounts.json`` (mode 0600), one entry
per provider. Every read-modify-write of that file holds an exclusive lock on
``accounts.json.lock``, so parallel processes (``review`` beside ``serve``) never
lose each other's entries or spend one single-use refresh token twice. Nothing
in this package logs or prints a token.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..api_utils import UnsafeEndpointError, validate_provider_endpoint

__all__ = [
    "ACCOUNTS_FILE_NAME",
    "OPENAI_CODEX_CLI_CLIENT_ID",
    "PROVIDERS",
    "REFRESH_SKEW_SECONDS",
    "XAI_GROK_CLI_CLIENT_ID",
    "AccountAuthError",
    "AccountStatus",
    "AccountTokens",
    "BearerResolution",
    "ProviderOAuthConfig",
    "account_status",
    "accounts_path",
    "clear_account_tokens",
    "client_id_for",
    "force_refresh",
    "form_body",
    "issuer_for",
    "load_account_tokens",
    "provider_oauth_config",
    "refresh_tokens",
    "resolve_bearer",
    "store_account_tokens",
]

# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

#: Public client id OpenAI's Codex CLI uses for the ChatGPT desktop OAuth flow
#: (an ``app_…`` id, not a secret). Disclosed in ``NOTICE``.
OPENAI_CODEX_CLI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
#: Public client id xAI publishes for the Grok CLI. Disclosed in ``NOTICE``.
XAI_GROK_CLI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

ACCOUNTS_FILE_NAME = "accounts.json"
ACCOUNTS_LOCK_NAME = "accounts.json.lock"
BEARER_TYPE = "Bearer"
#: Treat a token as expired this many seconds before its real expiry.
REFRESH_SKEW_SECONDS = 60
#: How long a process waits for another one's store write or refresh. A refresh
#: holds the lock for at most one token request (30 s client timeout).
STORE_LOCK_TIMEOUT_SECONDS = 45.0

#: OAuth error codes that end a grant for good: refreshing again cannot help,
#: only a new sign-in can (RFC 6749 section 5.2 plus provider spellings).
_DEFINITIVE_OAUTH_ERRORS = frozenset(
    {"invalid_grant", "invalid_token", "unauthorized_client", "access_denied"}
)
_DEFINITIVE_OAUTH_MARKERS = ("revoked", "expired", "reused", "invalidated")
#: Provider text kept in ``disabled_cause``: it is shown in the terminal, so
#: anything outside a plain error-code alphabet is dropped.
_CAUSE_UNSAFE = re.compile(r"[^a-z0-9_.\- ]")


@dataclass(frozen=True)
class ProviderOAuthConfig:
    """Everything that differs between providers, as one table row."""

    provider: str
    issuer_env: str
    default_issuer: str
    client_id_env: str
    default_client_id: str
    #: Path appended to the issuer for the token endpoint (refresh + code exchange).
    exchange_path: str
    scope: str
    #: Hosts that accept the account token as an API bearer.
    bearer_hosts: tuple[str, ...]
    #: Hosts of the same vendor that reject the account token: requests there
    #: warn and keep their "no key" behaviour instead of sending it.
    identity_only_hosts: tuple[str, ...] = ()

    @property
    def api_bearer_usable(self) -> bool:
        """True when some API host accepts the account token."""
        return bool(self.bearer_hosts)

    @property
    def api_hosts(self) -> tuple[str, ...]:
        """Every REST host this account is consulted for (usable or warned)."""
        return self.bearer_hosts + self.identity_only_hosts


OPENAI_OAUTH = ProviderOAuthConfig(
    provider="openai",
    issuer_env="SCREENSCRIBE_OPENAI_OAUTH_ISSUER",
    default_issuer="https://auth.openai.com",
    client_id_env="SCREENSCRIBE_OPENAI_OAUTH_CLIENT_ID",
    default_client_id=OPENAI_CODEX_CLI_CLIENT_ID,
    exchange_path="/oauth/token",
    scope="openid profile email offline_access",
    bearer_hosts=("chatgpt.com",),
    identity_only_hosts=("api.openai.com",),
)

XAI_OAUTH = ProviderOAuthConfig(
    provider="xai",
    issuer_env="SCREENSCRIBE_XAI_OAUTH_ISSUER",
    default_issuer="https://auth.x.ai",
    client_id_env="SCREENSCRIBE_XAI_OAUTH_CLIENT_ID",
    default_client_id=XAI_GROK_CLI_CLIENT_ID,
    exchange_path="/oauth2/token",
    scope="openid profile email offline_access grok-cli:access api:access",
    bearer_hosts=("api.x.ai",),
)

PROVIDERS: dict[str, ProviderOAuthConfig] = {
    OPENAI_OAUTH.provider: OPENAI_OAUTH,
    XAI_OAUTH.provider: XAI_OAUTH,
}


class AccountAuthError(Exception):
    """Account-auth failure. ``kind`` separates the operator's next action:

    ``unsupported_provider`` / ``no_client_id`` / ``not_signed_in`` / ``storage``
    / ``http`` (transport or a temporary provider failure: retry later) /
    ``oauth`` (the provider answered with a refusal) / ``revoked`` (the grant is
    gone for good: only a new ``screenscribe auth login`` helps).
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def provider_oauth_config(provider: str) -> ProviderOAuthConfig:
    """Registry row for ``provider`` or ``AccountAuthError(unsupported_provider)``."""
    row = PROVIDERS.get(provider.lower().strip())
    if row is None:
        raise AccountAuthError(
            "unsupported_provider",
            f"provider account auth is not available for {provider!r} "
            f"(supported: {', '.join(sorted(PROVIDERS))})",
        )
    return row


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def issuer_for(provider: str) -> str:
    """Resolve a TLS-only issuer, including explicitly configured overrides."""
    row = provider_oauth_config(provider)
    override = _non_empty(os.environ.get(row.issuer_env))
    issuer = override.rstrip("/") if override else row.default_issuer
    _validate_oauth_endpoint(issuer)
    return issuer


def _validate_oauth_endpoint(endpoint: str) -> None:
    """OAuth credentials never use plaintext, including on loopback."""
    try:
        validate_provider_endpoint(endpoint)
        if httpx.URL(endpoint).scheme != "https":
            raise UnsafeEndpointError("OAuth requires HTTPS")
    except UnsafeEndpointError:
        raise AccountAuthError("http", "OAuth endpoints require a valid HTTPS URL.") from None


def client_id_for(provider: str) -> str:
    """Client id: env override else the vendor-published default from the row."""
    row = provider_oauth_config(provider)
    override = _non_empty(os.environ.get(row.client_id_env))
    if override:
        return override
    if not row.default_client_id:
        raise AccountAuthError(
            "no_client_id", f"no OAuth client id for {provider}; set {row.client_id_env}"
        )
    return row.default_client_id


def form_body(pairs: list[tuple[str, str]]) -> str:
    """``application/x-www-form-urlencoded`` body with RFC 3986 escaping.

    Spaces become ``%20`` (not ``+``) -- the device-code grant type is a URN full
    of colons and mixing conventions is how these bodies silently fail to match
    server-side (same choice as the Rust reference).
    """
    return "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in pairs)


# --------------------------------------------------------------------------- #
# Tokens + store
# --------------------------------------------------------------------------- #


def _now() -> int:
    return int(time.time())


def _token_type(value: object) -> str:
    """``token_type`` from a provider body; defaults to ``Bearer`` when absent."""
    return value if isinstance(value, str) and value else BEARER_TYPE


@dataclass
class AccountTokens:
    """One provider's stored tokens. ``expires_at`` is absolute Unix seconds.

    ``authorized_at`` is stamped at sign-in and survives refreshes.
    ``disabled_cause`` is set when the provider ended the grant (revoked or
    expired refresh token); the entry stays for ``auth status`` until the next
    sign-in replaces it, but never yields a bearer again.
    """

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    token_type: str = BEARER_TYPE
    expires_at: int | None = None
    authorized_at: int | None = None
    disabled_cause: str | None = field(default=None)

    @classmethod
    def from_token_response(cls, body: dict[str, Any]) -> AccountTokens:
        """Build from a token-endpoint JSON body (``expires_in`` -> ``expires_at``)."""
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise AccountAuthError("oauth", "token response is missing access_token")
        expires_in = body.get("expires_in")
        expires_at: int | None = None
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
            expires_at = _now() + int(expires_in)
        elif isinstance(expires_in, str) and expires_in.strip().isdigit():
            expires_at = _now() + int(expires_in.strip())
        return cls(
            access_token=access,
            refresh_token=_non_empty(body.get("refresh_token"))
            if isinstance(body.get("refresh_token"), str)
            else None,
            id_token=_non_empty(body.get("id_token"))
            if isinstance(body.get("id_token"), str)
            else None,
            token_type=_token_type(body.get("token_type")),
            expires_at=expires_at,
        )

    def expires_within(self, skew: int = REFRESH_SKEW_SECONDS) -> bool:
        """True when the token dies within ``skew`` seconds. Unknown expiry = False."""
        if self.expires_at is None:
            return False
        return self.expires_at <= _now() + skew

    def still_valid(self) -> bool:
        """True while the access token itself has not expired (skew ignored)."""
        return self.expires_at is None or self.expires_at > _now()

    def __repr__(self) -> str:  # never leak a token through repr/logging
        return f"AccountTokens(token_type={self.token_type!r}, expires_at={self.expires_at!r}, has_refresh={self.refresh_token is not None})"


def accounts_path() -> Path:
    """``~/.config/screenscribe/accounts.json``."""
    return Path.home() / ".config" / "screenscribe" / ACCOUNTS_FILE_NAME


def _private_config_dir() -> Path:
    directory = accounts_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _try_exclusive_lock(fd: int) -> bool:
    """One non-blocking exclusive lock attempt on ``fd``; False while held elsewhere.

    ``fcntl`` is POSIX-only. The supported platforms are macOS and Linux, but
    the import stays local so a Windows checkout still imports this package
    (``msvcrt`` byte-range lock there) instead of failing every account lookup.
    """
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def _store_lock(timeout: float | None = None) -> Iterator[None]:
    """Exclusive cross-process lock for one read-modify-write of the store.

    ``flock`` conflicts between separate opens even inside one process, so
    threads serialize too. Never nest: a second acquire in the same thread
    waits for itself. Readers do not lock -- writes land by atomic rename.
    ``timeout`` defaults to :data:`STORE_LOCK_TIMEOUT_SECONDS`, read at call time.
    """
    wait = STORE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        lock_path = _private_config_dir() / ACCOUNTS_LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise AccountAuthError(
            "storage", f"cannot open the account store lock: {error.strerror or error}"
        ) from None
    try:
        deadline = time.monotonic() + wait
        while not _try_exclusive_lock(fd):
            if time.monotonic() >= deadline:
                raise AccountAuthError(
                    "storage",
                    f"{accounts_path()} is busy in another screenscribe process; retry",
                )
            time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _read_store() -> dict[str, Any]:
    path = accounts_path()
    if not path.exists():
        return {"version": 1, "accounts": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AccountAuthError("storage", f"cannot read {path}: {error}") from None
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    return {"version": 1, "accounts": accounts if isinstance(accounts, dict) else {}}


def _write_store(store: dict[str, Any]) -> None:
    """Atomic replace. Callers hold :func:`_store_lock` around read-modify-write.

    A filesystem failure (read-only or full config dir) is a ``storage``
    :class:`AccountAuthError`, never a raw ``OSError`` past the callers that
    only expect account errors.
    """
    path = accounts_path()
    try:
        _private_config_dir()
        fd, temp_name = tempfile.mkstemp(prefix=".accounts.json.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            os.chmod(temp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(store, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    except OSError as error:
        raise AccountAuthError(
            "storage", f"cannot write {path}: {error.strerror or error.__class__.__name__}"
        ) from None


def _store_writable() -> bool:
    """True when :func:`_write_store` can create its temp file next to the store."""
    return os.access(accounts_path().parent, os.W_OK | os.X_OK)


def _put_tokens(provider: str, tokens: AccountTokens) -> None:
    """Write one provider slot. Caller holds :func:`_store_lock`."""
    store = _read_store()
    store["accounts"][provider] = asdict(tokens)
    _write_store(store)


def store_account_tokens(provider: str, tokens: AccountTokens) -> None:
    """Persist ``tokens`` under the provider's own slot (0600 file).

    Other providers' slots written meanwhile by another process survive: the
    read-modify-write runs under the store lock.
    """
    row = provider_oauth_config(provider)
    with _store_lock():
        _put_tokens(row.provider, tokens)


def _optional_int(value: object) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(value)
    return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _stored_tokens(provider: str) -> AccountTokens | None:
    entry = _read_store()["accounts"].get(provider)
    if not isinstance(entry, dict) or not isinstance(entry.get("access_token"), str):
        return None
    return AccountTokens(
        access_token=entry["access_token"],
        refresh_token=_optional_str(entry.get("refresh_token")),
        id_token=_optional_str(entry.get("id_token")),
        token_type=_token_type(entry.get("token_type")),
        expires_at=_optional_int(entry.get("expires_at")),
        authorized_at=_optional_int(entry.get("authorized_at")),
        disabled_cause=_optional_str(entry.get("disabled_cause")),
    )


def load_account_tokens(provider: str) -> AccountTokens:
    """Stored tokens for ``provider`` or ``AccountAuthError(not_signed_in)``."""
    row = provider_oauth_config(provider)
    tokens = _stored_tokens(row.provider)
    if tokens is None:
        raise AccountAuthError("not_signed_in", f"not signed in to {row.provider}")
    return tokens


def clear_account_tokens(provider: str) -> bool:
    """Sign out of one provider. Returns True when an entry was removed."""
    row = provider_oauth_config(provider)
    with _store_lock():
        store = _read_store()
        removed = store["accounts"].pop(row.provider, None) is not None
        if removed or accounts_path().exists():
            _write_store(store)
    return removed


# --------------------------------------------------------------------------- #
# Identity (display only) + status
# --------------------------------------------------------------------------- #


def jwt_claims(token: str) -> dict[str, Any] | None:
    """Unverified JWT payload -- for display labels only, never authorization."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def id_token_identity(tokens: AccountTokens) -> str | None:
    """Best-effort ``email`` (else ``sub``) from the id_token, for display."""
    if not tokens.id_token:
        return None
    claims = jwt_claims(tokens.id_token)
    if not claims:
        return None
    for key in ("email", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class AccountStatus:
    provider: str
    signed_in: bool
    expires_at: int | None
    email: str | None
    api_bearer_usable: bool
    has_refresh_token: bool
    #: Hosts that accept the token (``chatgpt.com`` for OpenAI, ``api.x.ai``).
    bearer_hosts: tuple[str, ...] = ()
    authorized_at: int | None = None
    #: Why the provider ended the grant; set means "sign in again".
    disabled_cause: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= _now()

    @property
    def needs_login(self) -> bool:
        return self.disabled_cause is not None


def account_status(provider: str) -> AccountStatus:
    """Sign-in state for one provider. Never returns a token."""
    row = provider_oauth_config(provider)
    try:
        tokens = load_account_tokens(row.provider)
    except AccountAuthError as error:
        if error.kind != "not_signed_in":
            raise
        return AccountStatus(
            row.provider, False, None, None, row.api_bearer_usable, False, row.bearer_hosts
        )
    return AccountStatus(
        provider=row.provider,
        signed_in=True,
        expires_at=tokens.expires_at,
        email=id_token_identity(tokens),
        api_bearer_usable=row.api_bearer_usable,
        has_refresh_token=tokens.refresh_token is not None,
        bearer_hosts=row.bearer_hosts,
        authorized_at=tokens.authorized_at,
        disabled_cause=tokens.disabled_cause,
    )


# --------------------------------------------------------------------------- #
# Refresh + bearer resolution
# --------------------------------------------------------------------------- #


def _oauth_error_fields(response: httpx.Response) -> tuple[str, str]:
    """``(error, error_description)`` from an OAuth error body, lowercased."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    description = body.get("error_description")
    if isinstance(error, dict):  # OpenAI nests {"error": {"code", "message"}}
        description = error.get("message", description)
        error = error.get("code") or error.get("type")
    return (
        error.lower() if isinstance(error, str) else "",
        description.lower() if isinstance(description, str) else "",
    )


def _classify_refresh_failure(response: httpx.Response) -> AccountAuthError:
    """Definitive refusals end the grant (``revoked``); the rest stay retryable.

    429 and 5xx are temporary (``http``). Any 401 from the token endpoint is
    definitive: the refresh token itself was refused. A 400/403 is definitive
    when its error code or description names a dead grant -- the markers are
    matched against both, because OpenAI nests ``{"error": {"code":
    "refresh_token_reused", "message": ...}}`` with a message that need not
    repeat the marker. Other refusals stay ``oauth``: the grant is kept.
    """
    status = response.status_code
    if status == 429 or status >= 500:
        return AccountAuthError("http", f"refresh endpoint returned status {status}")
    error, description = _oauth_error_fields(response)
    names_dead_grant = error in _DEFINITIVE_OAUTH_ERRORS or any(
        marker in error or marker in description for marker in _DEFINITIVE_OAUTH_MARKERS
    )
    if status == 401 or (names_dead_grant and status in (400, 403)):
        code = _CAUSE_UNSAFE.sub("", error)[:64]
        reason = code or f"status {status}"
        return AccountAuthError("revoked", f"refresh refused: {reason}")
    return AccountAuthError("oauth", f"refresh endpoint returned status {status}")


def _request_refresh(
    row: ProviderOAuthConfig, tokens: AccountTokens, client: httpx.Client | None
) -> AccountTokens:
    """One ``grant_type=refresh_token`` request; returns the merged new tokens."""
    if not tokens.refresh_token:
        raise AccountAuthError("oauth", "stored account has no refresh token")
    endpoint = f"{issuer_for(row.provider)}{row.exchange_path}"
    body = form_body(
        [
            ("grant_type", "refresh_token"),
            ("client_id", client_id_for(row.provider)),
            ("refresh_token", tokens.refresh_token),
        ]
    )
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    own_client = client is None
    # Runs under the store lock: connect + write + read stays below
    # STORE_LOCK_TIMEOUT_SECONDS, so a waiting process sees the outcome
    # instead of a "busy" timeout.
    http = client or httpx.Client(timeout=httpx.Timeout(10.0, read=20.0))
    try:
        try:
            _validate_oauth_endpoint(endpoint)
            response = http.post(endpoint, content=body, headers=headers, follow_redirects=False)
        except httpx.HTTPError as error:
            raise AccountAuthError(
                "http", f"refresh request failed: {error.__class__.__name__}"
            ) from None
    finally:
        if own_client:
            http.close()
    if response.status_code >= 300:
        raise _classify_refresh_failure(response)
    try:
        payload = response.json()
    except ValueError:
        raise AccountAuthError("oauth", "refresh endpoint returned a non-JSON body") from None
    refreshed = AccountTokens.from_token_response(payload)
    # Providers that rotate the refresh token return a new one; those that do
    # not omit the field -- carry the old one forward.
    if refreshed.refresh_token is None:
        refreshed.refresh_token = tokens.refresh_token
    if refreshed.id_token is None:
        refreshed.id_token = tokens.id_token
    refreshed.authorized_at = tokens.authorized_at
    return refreshed


def _refresh_locked(
    row: ProviderOAuthConfig,
    seen_access_token: str,
    fallback: AccountTokens | None,
    client: httpx.Client | None,
    *,
    force: bool,
) -> AccountTokens:
    """Refresh under the store lock, reusing a peer's rotation when there is one.

    ``seen_access_token`` is the token the caller holds. When the store already
    has a different one, another process refreshed meanwhile: its token is
    returned without a network call (unless it is itself expiring and this is
    not a forced refresh after a 401). The newest stored refresh token is the
    one spent, so a rotated single-use refresh token is never replayed.

    With no stored entry (never stored, or signed out by another process after
    the caller loaded it) the caller's ``fallback`` tokens are refreshed but
    nothing is persisted: a sign-out is never undone by a late refresh.
    """
    # Refuse an unsafe issuer before touching the store or the network.
    _validate_oauth_endpoint(f"{issuer_for(row.provider)}{row.exchange_path}")
    with _store_lock():
        stored = _stored_tokens(row.provider)
        current = stored or fallback
        if current is None:
            raise AccountAuthError("not_signed_in", f"not signed in to {row.provider}")
        if current.disabled_cause:
            raise AccountAuthError("revoked", current.disabled_cause)
        peer_rotated = current.access_token != seen_access_token
        if peer_rotated and (force or not current.expires_within()):
            return current
        persist = stored is not None
        if persist and not _store_writable():
            # Spending a single-use refresh token whose successor cannot be
            # saved would kill the grant: refuse before the network call.
            raise AccountAuthError("storage", f"cannot write {accounts_path()}: read-only")
        try:
            refreshed = _request_refresh(row, current, client)
        except AccountAuthError as error:
            if error.kind == "revoked" and persist:
                current.disabled_cause = str(error)
                _put_tokens(row.provider, current)
            raise
        if persist:
            _put_tokens(row.provider, refreshed)
        return refreshed


def refresh_tokens(
    provider: str,
    tokens: AccountTokens,
    *,
    client: httpx.Client | None = None,
) -> AccountTokens:
    """``grant_type=refresh_token`` (form) on the row's token endpoint; stores result.

    Serialized across processes; a peer's fresh rotation is reused instead of
    spending the refresh token again. A definitive refusal marks the stored
    account as needing a new sign-in (``AccountAuthError("revoked")``).
    """
    row = provider_oauth_config(provider)
    return _refresh_locked(row, tokens.access_token, tokens, client, force=False)


def force_refresh(
    provider: str,
    rejected_access_token: str,
    *,
    client: httpx.Client | None = None,
) -> AccountTokens:
    """Refresh after the API rejected ``rejected_access_token`` (HTTP 401).

    When another process already replaced that token, the replacement is
    returned as-is; otherwise the grant is refreshed once, whatever the
    stored expiry says.
    """
    row = provider_oauth_config(provider)
    return _refresh_locked(row, rejected_access_token, None, client, force=True)


OPENAI_IDENTITY_ONLY_WARNING = (
    "OpenAI account sign-in is identity-only on api.openai.com: that API rejects "
    "ChatGPT account tokens (verified by Codescribe 2026-09-08). Use the "
    "ChatGPT route for LLM/vision (`screenscribe config auth openai --login`), or "
    "set an OpenAI API key (OPENAI_API_KEY / SCREENSCRIBE_*_API_KEY) for api.openai.com."
)


@dataclass(frozen=True)
class BearerResolution:
    """Outcome of :func:`resolve_bearer`. ``bearer`` may be an account token that
    is *not* usable as an API bearer -- check ``identity_only`` before use."""

    bearer: str = field(repr=False)  # never leak a token through repr/logging
    source: str  # "explicit" | "account" | "none"
    identity_only: bool = False
    warning: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.bearer) and not self.identity_only


def _login_hint(provider: str) -> str:
    return f"run `screenscribe auth login {provider}`"


def resolve_bearer(
    provider: str,
    explicit_key: str = "",
    *,
    host: str | None = None,
    client: httpx.Client | None = None,
) -> BearerResolution:
    """Explicit key wins. Else the signed-in account token, refreshed when it is
    about to expire and a refresh token exists.

    ``host`` is the actual destination. The token is a usable bearer only on the
    row's ``bearer_hosts``; anywhere else (``api.openai.com`` for OpenAI) it comes
    back ``identity_only=True`` with a warning and callers keep "no key". Without
    ``host`` the row decides (OpenAI: identity only, the historical contract).

    A temporary refresh failure (network, 5xx, 429) keeps using an access token
    that has not actually expired yet; a definitive one (revoked or expired
    grant) yields no bearer and a sign-in hint.
    """
    if explicit_key:
        return BearerResolution(bearer=explicit_key, source="explicit")
    row = provider_oauth_config(provider)
    try:
        tokens = load_account_tokens(row.provider)
    except AccountAuthError as error:
        if error.kind == "not_signed_in":
            return BearerResolution(bearer="", source="none")
        raise
    if tokens.disabled_cause:
        return BearerResolution(
            bearer="",
            source="none",
            warning=f"{row.provider} account sign-in has ended ({tokens.disabled_cause}); "
            f"{_login_hint(row.provider)}",
        )
    usable_here = host in row.bearer_hosts if host is not None else not row.identity_only_hosts
    if not usable_here:
        # Nothing will be sent: never spend the lock or a refresh token on it.
        return BearerResolution(
            bearer=tokens.access_token,
            source="account",
            identity_only=True,
            warning=OPENAI_IDENTITY_ONLY_WARNING if row.provider == "openai" else None,
        )
    warning: str | None = None
    if tokens.expires_within():
        if not tokens.refresh_token:
            if not tokens.still_valid():
                return BearerResolution(
                    bearer="",
                    source="none",
                    warning=f"{row.provider} account token expired and no refresh token is "
                    f"stored; {_login_hint(row.provider)}",
                )
        else:
            try:
                tokens = refresh_tokens(row.provider, tokens, client=client)
            except AccountAuthError as error:
                if error.kind == "revoked" or not tokens.still_valid():
                    return BearerResolution(
                        bearer="",
                        source="none",
                        warning=f"{row.provider} account token refresh failed ({error.kind}); "
                        f"{_login_hint(row.provider)}",
                    )
                warning = (
                    f"{row.provider} account token refresh failed ({error.kind}); using the "
                    "current token until it expires"
                )
    return BearerResolution(bearer=tokens.access_token, source="account", warning=warning)
