"""Provider-account (OAuth) authentication for Screenscribe.

Port of Codescribe ``core/llm/account_auth`` (Rust, read-only reference) to a
file-backed Python store. Two providers can sign in with an account:

- ``openai`` -- Codex device flow (``{issuer}/api/accounts/deviceauth/*`` then a
  PKCE code exchange on ``{issuer}/oauth/token``). **Identity only**: Codescribe
  verified on 2026-09-08 that a ChatGPT account token is rejected by the
  ``api.openai.com`` REST surface (no Codex-backend route), so Screenscribe never
  sends it as a bearer for STT/LLM calls -- see :func:`resolve_bearer`.
- ``xai`` -- RFC 8628 device authorization on ``https://auth.x.ai``. The token
  carries ``api:access`` and is usable on ``api.x.ai``.

Tokens live in ``~/.config/screenscribe/accounts.json`` (mode 0600), one entry
per provider. Nothing in this package logs or prints a token.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

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
BEARER_TYPE = "Bearer"
#: Treat a token as expired this many seconds before its real expiry.
REFRESH_SKEW_SECONDS = 60


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
    #: True when the account token is usable as an API bearer on the provider's
    #: REST host. False = identity/status only (OpenAI, see module docstring).
    api_bearer_usable: bool
    #: REST hosts whose requests may fall back to this account's bearer.
    api_hosts: tuple[str, ...]


OPENAI_OAUTH = ProviderOAuthConfig(
    provider="openai",
    issuer_env="SCREENSCRIBE_OPENAI_OAUTH_ISSUER",
    default_issuer="https://auth.openai.com",
    client_id_env="SCREENSCRIBE_OPENAI_OAUTH_CLIENT_ID",
    default_client_id=OPENAI_CODEX_CLI_CLIENT_ID,
    exchange_path="/oauth/token",
    scope="openid profile email offline_access",
    api_bearer_usable=False,
    api_hosts=("api.openai.com",),
)

XAI_OAUTH = ProviderOAuthConfig(
    provider="xai",
    issuer_env="SCREENSCRIBE_XAI_OAUTH_ISSUER",
    default_issuer="https://auth.x.ai",
    client_id_env="SCREENSCRIBE_XAI_OAUTH_CLIENT_ID",
    default_client_id=XAI_GROK_CLI_CLIENT_ID,
    exchange_path="/oauth2/token",
    scope="openid profile email offline_access grok-cli:access api:access",
    api_bearer_usable=True,
    api_hosts=("api.x.ai",),
)

PROVIDERS: dict[str, ProviderOAuthConfig] = {
    OPENAI_OAUTH.provider: OPENAI_OAUTH,
    XAI_OAUTH.provider: XAI_OAUTH,
}


class AccountAuthError(Exception):
    """Account-auth failure. ``kind`` separates the operator's next action:

    ``unsupported_provider`` / ``no_client_id`` / ``not_signed_in`` / ``storage``
    / ``http`` (transport) / ``oauth`` (the provider answered with a refusal).
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
    """Issuer base URL: env override (trailing slash stripped) else the row default."""
    row = provider_oauth_config(provider)
    override = _non_empty(os.environ.get(row.issuer_env))
    return override.rstrip("/") if override else row.default_issuer


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
    """One provider's stored tokens. ``expires_at`` is absolute Unix seconds."""

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    token_type: str = BEARER_TYPE
    expires_at: int | None = None

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

    def __repr__(self) -> str:  # never leak a token through repr/logging
        return f"AccountTokens(token_type={self.token_type!r}, expires_at={self.expires_at!r}, has_refresh={self.refresh_token is not None})"


def accounts_path() -> Path:
    """``~/.config/screenscribe/accounts.json``."""
    return Path.home() / ".config" / "screenscribe" / ACCOUNTS_FILE_NAME


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
    path = accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
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


def store_account_tokens(provider: str, tokens: AccountTokens) -> None:
    """Persist ``tokens`` under the provider's own slot (0600 file)."""
    row = provider_oauth_config(provider)
    store = _read_store()
    store["accounts"][row.provider] = asdict(tokens)
    _write_store(store)


def load_account_tokens(provider: str) -> AccountTokens:
    """Stored tokens for ``provider`` or ``AccountAuthError(not_signed_in)``."""
    row = provider_oauth_config(provider)
    entry = _read_store()["accounts"].get(row.provider)
    if not isinstance(entry, dict) or not isinstance(entry.get("access_token"), str):
        raise AccountAuthError("not_signed_in", f"not signed in to {row.provider}")
    expires_at = entry.get("expires_at")
    return AccountTokens(
        access_token=entry["access_token"],
        refresh_token=entry.get("refresh_token")
        if isinstance(entry.get("refresh_token"), str)
        else None,
        id_token=entry.get("id_token") if isinstance(entry.get("id_token"), str) else None,
        token_type=_token_type(entry.get("token_type")),
        expires_at=int(expires_at)
        if isinstance(expires_at, int | float) and not isinstance(expires_at, bool)
        else None,
    )


def clear_account_tokens(provider: str) -> bool:
    """Sign out of one provider. Returns True when an entry was removed."""
    row = provider_oauth_config(provider)
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

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= _now()


def account_status(provider: str) -> AccountStatus:
    """Sign-in state for one provider. Never returns a token."""
    row = provider_oauth_config(provider)
    try:
        tokens = load_account_tokens(row.provider)
    except AccountAuthError as error:
        if error.kind != "not_signed_in":
            raise
        return AccountStatus(row.provider, False, None, None, row.api_bearer_usable, False)
    return AccountStatus(
        provider=row.provider,
        signed_in=True,
        expires_at=tokens.expires_at,
        email=id_token_identity(tokens),
        api_bearer_usable=row.api_bearer_usable,
        has_refresh_token=tokens.refresh_token is not None,
    )


# --------------------------------------------------------------------------- #
# Refresh + bearer resolution
# --------------------------------------------------------------------------- #


def refresh_tokens(
    provider: str,
    tokens: AccountTokens,
    *,
    client: httpx.Client | None = None,
) -> AccountTokens:
    """``grant_type=refresh_token`` (form) on the row's token endpoint; stores result."""
    row = provider_oauth_config(provider)
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
    http = client or httpx.Client(timeout=30.0)
    try:
        try:
            response = http.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as error:
            raise AccountAuthError(
                "http", f"refresh request failed: {error.__class__.__name__}"
            ) from None
    finally:
        if own_client:
            http.close()
    if response.status_code >= 400:
        raise AccountAuthError("oauth", f"refresh endpoint returned status {response.status_code}")
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
    store_account_tokens(row.provider, refreshed)
    return refreshed


OPENAI_IDENTITY_ONLY_WARNING = (
    "OpenAI account sign-in is identity-only: api.openai.com rejects a ChatGPT "
    "account token for REST calls (verified by Codescribe 2026-09-08). Set an "
    "OpenAI API key (OPENAI_API_KEY / SCREENSCRIBE_*_API_KEY) for STT/LLM/Vision."
)


@dataclass(frozen=True)
class BearerResolution:
    """Outcome of :func:`resolve_bearer`. ``bearer`` may be an account token that
    is *not* usable as an API bearer -- check ``identity_only`` before use."""

    bearer: str
    source: str  # "explicit" | "account" | "none"
    identity_only: bool = False
    warning: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.bearer) and not self.identity_only


def resolve_bearer(
    provider: str,
    explicit_key: str = "",
    *,
    client: httpx.Client | None = None,
) -> BearerResolution:
    """Explicit key wins. Else the signed-in account token (refreshed when expiring
    and a refresh token exists). For ``openai`` the token comes back tagged
    ``identity_only=True`` with a warning; callers must fall back to an API key.
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
    if tokens.expires_within():
        if not tokens.refresh_token:
            return BearerResolution(
                bearer="",
                source="none",
                warning=f"{row.provider} account token expired and no refresh token is stored; run `screenscribe auth login {row.provider}`",
            )
        try:
            tokens = refresh_tokens(row.provider, tokens, client=client)
        except AccountAuthError as error:
            return BearerResolution(
                bearer="",
                source="none",
                warning=f"{row.provider} account token refresh failed ({error.kind}); run `screenscribe auth login {row.provider}`",
            )
    if not row.api_bearer_usable:
        return BearerResolution(
            bearer=tokens.access_token,
            source="account",
            identity_only=True,
            warning=OPENAI_IDENTITY_ONLY_WARNING,
        )
    return BearerResolution(bearer=tokens.access_token, source="account")
