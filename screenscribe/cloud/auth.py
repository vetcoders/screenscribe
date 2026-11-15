"""Accounts, dual auth, Sign in with Apple and RFC 8628 device flow.

Two credential transports reach the same ``sessions`` table:

- ``Authorization: Bearer <access>`` for the macOS app and the CLI. Ambient
  cookies are ignored whenever a bearer header is present, so these clients
  are immune to CSRF.
- HttpOnly ``ss_session`` cookie for the web portal. Every state-changing
  cookie request must pass an ``Origin``/``Referer`` host check AND a CSRF
  double-submit (``X-CSRF-Token`` header or ``_csrf_token`` form field
  matching the readable ``ss_csrf`` cookie).

Only SHA-256 hashes of opaque credentials are persisted.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError

from .config import CloudSettings
from .db import CloudStore, DevicePollOutcome, RefreshOutcome, SessionRecord, TokenRows, UserRecord
from .jws import AppleJwsVerifier, JwsError, StoreKitVerifier
from .schemas import (
    ACCESS_EXPIRES_IN,
    BEARER_SCHEME,
    DEVICE_GRANT,
    REFRESH_GRANT,
    AppleSignInRequest,
    LoginRequest,
    RegisterRequest,
)
from .security import (
    ACCESS_PREFIX,
    CSRF_COOKIE,
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    DEVICE_PREFIX,
    REFRESH_PREFIX,
    SAFE_METHODS,
    SESSION_COOKIE,
    WEB_PREFIX,
    credential_hash,
    csrf_matches,
    hash_password,
    is_valid_email,
    isoformat_z,
    new_csrf_value,
    new_opaque_credential,
    new_user_code,
    normalize_email,
    origin_allowed,
    safe_next_path,
    utc_now,
    verify_password,
)

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_ID = "screenscribe-cli"
_MAX_FORM_BYTES = 64 * 1024
# Unknown emails are verified against a lazily computed scrypt hash of this
# throwaway value, so login timing does not reveal whether an account exists.
_DUMMY_HASH_SEED = "screenscribe-cloud-timing-equalizer"


# -- Service container -------------------------------------------------------


@dataclass
class CloudServices:
    """Per-app dependencies, injectable for tests (``create_cloud_app(...)``)."""

    settings: CloudSettings
    store: CloudStore
    apple_verifier: AppleJwsVerifier
    storekit_verifier: StoreKitVerifier
    http_transport: httpx.AsyncBaseTransport | None = None
    duration_probe: Callable[[Path], float] | None = None
    clock: Callable[[], datetime] = utc_now
    dummy_password_hash: str = field(default="", repr=False)

    def now(self) -> datetime:
        return self.clock()

    def http_client(self) -> httpx.AsyncClient:
        """Outbound client with a finite timeout (Stripe, Apple)."""
        return httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), transport=self.http_transport)


def get_services(request: Request) -> CloudServices:
    return cast(CloudServices, request.app.state.cloud)


@dataclass(frozen=True)
class AuthContext:
    user: UserRecord
    session: SessionRecord
    via_cookie: bool
    credential_hash: str


# -- Credential parsing ------------------------------------------------------


def bearer_credential(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _resolve(services: CloudServices, presented: str, via_cookie: bool) -> AuthContext | None:
    digest = credential_hash(presented)
    session = services.store.get_active_session(digest, services.now())
    if session is None:
        return None
    if via_cookie and session.transport != "cookie":
        return None
    user = services.store.get_user(session.user_id)
    if user is None:
        return None
    return AuthContext(user=user, session=session, via_cookie=via_cookie, credential_hash=digest)


async def resolve_auth(request: Request) -> AuthContext | None:
    """Authenticate via Bearer (preferred) or the web session cookie."""
    services = get_services(request)
    bearer = bearer_credential(request)
    if bearer is not None:
        return await run_in_threadpool(_resolve, services, bearer, False)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return await run_in_threadpool(_resolve, services, cookie, True)
    return None


def _content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def submitted_csrf(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    if _content_type(request) == "application/x-www-form-urlencoded":
        form = await read_form_or_json(request)
        return form.get(CSRF_FORM_FIELD)
    return None


async def enforce_csrf(request: Request) -> None:
    """Origin/Referer host match + CSRF double-submit for cookie writes."""
    services = get_services(request)
    if not origin_allowed(request.headers, services.settings.allowed_origin_hosts()):
        raise HTTPException(status_code=403, detail="Cross-site request rejected.")
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), await submitted_csrf(request)):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")


async def optional_auth(request: Request) -> AuthContext | None:
    """Like :func:`require_user` but anonymous requests yield ``None``."""
    ctx = await resolve_auth(request)
    if ctx is not None and ctx.via_cookie and request.method.upper() not in SAFE_METHODS:
        await enforce_csrf(request)
    return ctx


async def require_user(request: Request) -> AuthContext:
    ctx = await resolve_auth(request)
    if ctx is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": BEARER_SCHEME},
        )
    if ctx.via_cookie and request.method.upper() not in SAFE_METHODS:
        await enforce_csrf(request)
    return ctx


CurrentAuth = Annotated[AuthContext, Depends(require_user)]
MaybeAuth = Annotated[AuthContext | None, Depends(optional_auth)]


# -- Body parsing (form OR JSON, as RFC 8628 / RFC 6749 clients send) -------


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


async def read_form_or_json(request: Request) -> dict[str, str]:
    """Parse a small urlencoded or JSON body into ``{str: str}``.

    Cached on ``request.state`` so CSRF checks and handlers share one parse.
    """
    cached: dict[str, str] | None = getattr(request.state, "parsed_body", None)
    if cached is not None:
        return cached
    raw = await request.body()
    if len(raw) > _MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large.")
    ctype = _content_type(request)
    result: dict[str, str] = {}
    stripped = raw.strip()
    if ctype == "application/json" or (not ctype and stripped.startswith(b"{")):
        if stripped:
            try:
                data = json.loads(stripped)
            except (ValueError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=400, detail="Malformed JSON body.") from exc
            if not isinstance(data, dict):
                raise HTTPException(status_code=400, detail="JSON body must be an object.")
            for key, value in data.items():
                scalar = _scalar(value)
                if scalar is not None:
                    result[str(key)] = scalar
    elif ctype in ("application/x-www-form-urlencoded", ""):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Body must be UTF-8.") from exc
        for key, value in parse_qsl(text, keep_blank_values=True):
            result[key] = value
    else:
        raise HTTPException(status_code=415, detail="Use JSON or form encoding.")
    request.state.parsed_body = result
    return result


def is_form_post(request: Request) -> bool:
    return _content_type(request) == "application/x-www-form-urlencoded"


def _validate(model: type[BaseModel], data: dict[str, Any]) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# -- Tokens & cookies --------------------------------------------------------


@dataclass(frozen=True)
class IssuedTokens:
    access: str
    refresh: str
    rows: TokenRows


def mint_tokens(settings: CloudSettings, now: datetime, transport: str) -> IssuedTokens:
    access = new_opaque_credential(ACCESS_PREFIX)
    refresh = new_opaque_credential(REFRESH_PREFIX)
    rows = TokenRows(
        access_hash=credential_hash(access),
        refresh_hash=credential_hash(refresh),
        access_expires_at=isoformat_z(now + timedelta(seconds=ACCESS_EXPIRES_IN)),
        refresh_expires_at=isoformat_z(now + timedelta(seconds=settings.refresh_ttl_seconds)),
        transport=transport,
    )
    return IssuedTokens(access=access, refresh=refresh, rows=rows)


def token_body(tokens: IssuedTokens, user: UserRecord | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": tokens.access,
        "refresh_token": tokens.refresh,
        "token_type": BEARER_SCHEME,
        "expires_in": ACCESS_EXPIRES_IN,
    }
    if user is not None:
        body["user"] = user.public()
    return body


def issue_bundle(services: CloudServices, user: UserRecord, transport: str = "bearer") -> IssuedTokens:
    tokens = mint_tokens(services.settings, services.now(), transport)
    services.store.issue_token_pair(user.user_id, tokens.rows, services.now())
    return tokens


def start_web_session(services: CloudServices, user: UserRecord) -> tuple[str, str]:
    """Create a cookie session; returns ``(session_value, csrf_value)``."""
    now = services.now()
    value = new_opaque_credential(WEB_PREFIX)
    services.store.create_session(
        user_id=user.user_id,
        token_hash=credential_hash(value),
        transport="cookie",
        expires_at=isoformat_z(now + timedelta(seconds=services.settings.web_session_ttl_seconds)),
        now=now,
    )
    return value, new_csrf_value()


def set_session_cookies(
    response: Response, settings: CloudSettings, session_value: str, csrf_value: str
) -> None:
    max_age = settings.web_session_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE,
        session_value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_value,
        max_age=max_age,
        path="/",
        httponly=False,
        samesite="lax",
        secure=settings.secure_cookies,
    )


def ensure_csrf_cookie(request: Request, response: Response, settings: CloudSettings) -> str:
    """Return the current CSRF value, minting the cookie when absent."""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing:
        return existing
    value = new_csrf_value()
    response.set_cookie(
        CSRF_COOKIE,
        value,
        max_age=settings.web_session_ttl_seconds,
        path="/",
        httponly=False,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return value


def clear_session_cookies(response: Response, settings: CloudSettings) -> None:
    for name, httponly in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(
            name, path="/", httponly=httponly, samesite="lax", secure=settings.secure_cookies
        )


# -- Account operations (sync; run in the threadpool) -----------------------


def _preferred_lang(value: str | None, default: str) -> str:
    lang = (value or "").strip().lower()
    return lang if lang in {"en", "pl"} else default


def register_account(services: CloudServices, payload: RegisterRequest) -> UserRecord | None:
    email = normalize_email(payload.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return services.store.create_user(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=(payload.display_name or "").strip(),
        preferred_lang=_preferred_lang(payload.preferred_lang, services.settings.default_ui_language),
        now=services.now(),
    )


def authenticate(services: CloudServices, email: str, password: str) -> UserRecord | None:
    user = services.store.get_user_by_email(normalize_email(email))
    if user is None or not user.password_hash:
        if not services.dummy_password_hash:
            services.dummy_password_hash = hash_password(_DUMMY_HASH_SEED)
        verify_password(password, services.dummy_password_hash)
        return None
    return user if verify_password(password, user.password_hash) else None


def apple_upsert(
    services: CloudServices, claims: dict[str, Any], hinted_email: str | None, display_name: str
) -> UserRecord:
    """Find or create the account bound to the Apple ``sub``.

    A verified email from the identity token links an existing password
    account; an unverified client-supplied email is only used for a brand-new
    account and never links to someone else's account.
    """
    store = services.store
    now = services.now()
    sub = str(claims["sub"])
    existing = store.get_user_by_apple_sub(sub)
    if existing is not None:
        return existing
    token_email = normalize_email(str(claims.get("email") or ""))
    verified = str(claims.get("email_verified", "")).lower() in {"true", "1"}
    if token_email and verified:
        by_email = store.get_user_by_email(token_email)
        if by_email is not None:
            if by_email.apple_sub is None and store.link_apple_sub(by_email.user_id, sub, now):
                linked = store.get_user(by_email.user_id)
                if linked is not None:
                    return linked
            raise HTTPException(status_code=409, detail="Email is linked to another Apple ID.")
    candidates = [token_email, normalize_email(hinted_email or "")]
    for email in candidates:
        if email and is_valid_email(email) and store.get_user_by_email(email) is None:
            created = store.create_user(
                email=email,
                password_hash=None,
                display_name=display_name,
                preferred_lang=services.settings.default_ui_language,
                now=now,
                apple_sub=sub,
            )
            if created is not None:
                return created
    placeholder = f"apple-{credential_hash(sub)[:20]}@users.invalid"
    created = store.create_user(
        email=placeholder,
        password_hash=None,
        display_name=display_name,
        preferred_lang=services.settings.default_ui_language,
        now=now,
        apple_sub=sub,
    )
    if created is None:
        raced = store.get_user_by_apple_sub(sub)
        if raced is None:
            raise HTTPException(status_code=409, detail="Could not create the Apple account.")
        return raced
    return created


def me_body(services: CloudServices, user: UserRecord) -> dict[str, Any]:
    """Exact ``GET /api/v1/auth/me`` body."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "subscription": services.store.subscription_snapshot(user.user_id, services.now()),
    }


def create_device_authorization(services: CloudServices, client_id: str) -> dict[str, Any]:
    settings = services.settings
    now = services.now()
    expires_at = isoformat_z(now + timedelta(seconds=settings.device_code_ttl_seconds))
    for _attempt in range(8):
        device_code = new_opaque_credential(DEVICE_PREFIX)
        user_code = new_user_code()
        if services.store.create_device_code(
            device_code_hash=credential_hash(device_code),
            user_code=user_code,
            client_id=client_id,
            interval_seconds=settings.device_poll_interval,
            expires_at=expires_at,
            now=now,
        ):
            verification_uri = settings.url("/activate")
            return {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": verification_uri,
                "verification_uri_complete": f"{verification_uri}?{urlencode({'user_code': user_code})}",
                "expires_in": settings.device_code_ttl_seconds,
                "interval": settings.device_poll_interval,
            }
    raise HTTPException(status_code=503, detail="Could not allocate a device code. Retry.")


def _oauth_error(code: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _token_response(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# -- Router ------------------------------------------------------------------


def _remove_child_dir(root: Path, leaf: str) -> None:
    if not leaf or "/" in leaf or "\\" in leaf or leaf in {".", ".."}:
        return
    base = root.resolve()
    candidate = (base / leaf).resolve()
    if candidate.parent == base and candidate.exists():
        shutil.rmtree(candidate, ignore_errors=True)


def purge_account_storage(services: CloudServices, share_ids: list[str], job_ids: list[str]) -> None:
    for share_id in share_ids:
        _remove_child_dir(services.settings.reviews_dir, share_id)
    for job_id in job_ids:
        _remove_child_dir(services.settings.uploads_dir, job_id)


def _login_redirect(next_path: str, error: str) -> RedirectResponse:
    query = urlencode({"next": next_path, "error": error})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


def build_auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    @router.post("/register", status_code=201)
    async def register(request: Request) -> Response:
        services = get_services(request)
        data = await read_form_or_json(request)
        form = is_form_post(request)
        next_path = safe_next_path(data.get("next"))
        if form:
            if not origin_allowed(request.headers, services.settings.allowed_origin_hosts()):
                raise HTTPException(status_code=403, detail="Cross-site request rejected.")
            try:
                payload = RegisterRequest.model_validate(data)
            except ValidationError:
                return _login_redirect(next_path, "register_invalid")
        else:
            payload = cast(RegisterRequest, _validate(RegisterRequest, dict(data)))
        user = await run_in_threadpool(register_account, services, payload)
        if user is None:
            if form:
                return _login_redirect(next_path, "email_taken")
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        tokens = await run_in_threadpool(issue_bundle, services, user)
        response: Response
        if form:
            response = RedirectResponse(url=next_path, status_code=303)
        else:
            response = _token_response(token_body(tokens, user), status_code=201)
        if form or payload.cookie:
            session_value, csrf_value = await run_in_threadpool(start_web_session, services, user)
            set_session_cookies(response, services.settings, session_value, csrf_value)
        return response

    @router.post("/login")
    async def login(request: Request) -> Response:
        services = get_services(request)
        data = await read_form_or_json(request)
        form = is_form_post(request)
        next_path = safe_next_path(data.get("next"))
        if form:
            if not origin_allowed(request.headers, services.settings.allowed_origin_hosts()):
                raise HTTPException(status_code=403, detail="Cross-site request rejected.")
            try:
                payload = LoginRequest.model_validate(data)
            except ValidationError:
                return _login_redirect(next_path, "invalid_credentials")
        else:
            payload = cast(LoginRequest, _validate(LoginRequest, dict(data)))
        user = await run_in_threadpool(authenticate, services, payload.email, payload.password)
        if user is None:
            if form:
                return _login_redirect(next_path, "invalid_credentials")
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        tokens = await run_in_threadpool(issue_bundle, services, user)
        response: Response
        if form:
            response = RedirectResponse(url=next_path, status_code=303)
        else:
            response = _token_response(token_body(tokens, user))
        if form or payload.cookie:
            session_value, csrf_value = await run_in_threadpool(start_web_session, services, user)
            set_session_cookies(response, services.settings, session_value, csrf_value)
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        services = get_services(request)
        ctx = await resolve_auth(request)
        if ctx is not None:
            if ctx.via_cookie:
                await enforce_csrf(request)
            await run_in_threadpool(services.store.revoke_session, ctx.credential_hash, services.now())
        response: Response
        if is_form_post(request):
            response = RedirectResponse(url="/", status_code=303)
        else:
            response = JSONResponse(content={"ok": True})
        clear_session_cookies(response, services.settings)
        return response

    @router.get("/me")
    async def me(request: Request, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        return await run_in_threadpool(me_body, services, auth.user)

    @router.post("/apple")
    async def apple_sign_in(request: Request) -> Response:
        services = get_services(request)
        data = await read_form_or_json(request)
        payload = cast(AppleSignInRequest, _validate(AppleSignInRequest, dict(data)))
        audiences = services.settings.apple_audiences
        if not audiences:
            raise HTTPException(status_code=503, detail="Sign in with Apple is not configured.")
        try:
            claims = await services.apple_verifier.verify_identity_token(
                payload.identity_token, audiences=audiences
            )
        except JwsError as exc:
            logger.info("Apple identity token rejected: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid Apple identity token.") from exc
        except httpx.HTTPError as exc:
            logger.warning("Apple JWKS unavailable: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Apple sign-in is temporarily unavailable.") from exc
        user = await run_in_threadpool(
            apple_upsert, services, claims, payload.email, (payload.display_name or "").strip()
        )
        tokens = await run_in_threadpool(issue_bundle, services, user)
        return _token_response(token_body(tokens, user))

    @router.post("/device/code")
    async def device_code(request: Request) -> Response:
        services = get_services(request)
        data = await read_form_or_json(request)
        client_id = (data.get("client_id") or DEFAULT_CLIENT_ID).strip()[:64] or DEFAULT_CLIENT_ID
        body = await run_in_threadpool(create_device_authorization, services, client_id)
        return _token_response(body)

    @router.post("/device/token")
    async def device_token(request: Request) -> Response:
        services = get_services(request)
        data = await read_form_or_json(request)
        if data.get("grant_type") != DEVICE_GRANT:
            return _oauth_error("unsupported_grant_type")
        presented = (data.get("device_code") or "").strip()
        if not presented:
            return _oauth_error("invalid_request")
        tokens = mint_tokens(services.settings, services.now(), "device")
        outcome: DevicePollOutcome = await run_in_threadpool(
            services.store.poll_device_code, credential_hash(presented), tokens.rows, services.now()
        )
        if outcome.status != "ok":
            return _oauth_error(outcome.status)
        return _token_response(token_body(tokens))

    @router.post("/token/refresh")
    @router.post("/refresh", include_in_schema=False)
    async def refresh(request: Request) -> Response:
        services = get_services(request)
        data = await read_form_or_json(request)
        grant_type = data.get("grant_type")
        if grant_type is not None and grant_type != REFRESH_GRANT:
            return _oauth_error("unsupported_grant_type")
        presented = (data.get("refresh_token") or "").strip()
        if not presented:
            return _oauth_error("invalid_request")
        tokens = mint_tokens(services.settings, services.now(), "bearer")
        outcome: RefreshOutcome = await run_in_threadpool(
            services.store.rotate_refresh_token, credential_hash(presented), tokens.rows, services.now()
        )
        if outcome.status != "ok":
            if outcome.status == "reused":
                logger.warning("Refresh token reuse detected; token family revoked.")
            return _oauth_error("invalid_grant")
        return _token_response(token_body(tokens))

    @router.post("/account/delete")
    @router.delete("/account")
    async def delete_account(request: Request, auth: CurrentAuth) -> Response:
        services = get_services(request)
        user_id = auth.user.user_id
        purged = await run_in_threadpool(services.store.delete_user, user_id)
        if purged is not None:
            share_ids, job_ids = purged
            registry = getattr(request.app.state, "review_registry", None)
            if registry is not None:
                for share_id in share_ids:
                    registry.drop(share_id)
            await run_in_threadpool(purge_account_storage, services, share_ids, job_ids)
        response: Response
        if is_form_post(request):
            response = RedirectResponse(url="/", status_code=303)
        else:
            response = JSONResponse(content={"deleted": True, "user_id": user_id})
        clear_session_cookies(response, services.settings)
        return response

    return router


__all__ = [
    "DEFAULT_CLIENT_ID",
    "AuthContext",
    "CloudServices",
    "CurrentAuth",
    "IssuedTokens",
    "MaybeAuth",
    "apple_upsert",
    "authenticate",
    "bearer_credential",
    "build_auth_router",
    "clear_session_cookies",
    "create_device_authorization",
    "enforce_csrf",
    "ensure_csrf_cookie",
    "get_services",
    "is_form_post",
    "issue_bundle",
    "me_body",
    "mint_tokens",
    "optional_auth",
    "read_form_or_json",
    "register_account",
    "require_user",
    "resolve_auth",
    "set_session_cookies",
    "start_web_session",
    "token_body",
]
