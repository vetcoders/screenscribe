"""Device-code sign-in for ``openai`` (Codex device flow) and ``xai`` (RFC 8628).

Ported from Codescribe ``core/llm/account_auth/device_code.rs`` and
``server.rs::exchange_code_for_tokens``. The two wire shapes are not
interchangeable:

- **OpenAI Codex** -- JSON ``POST {issuer}/api/accounts/deviceauth/usercode`` ->
  ``{user_code, device_auth_id, interval}``; the user opens
  ``{issuer}/codex/device``; poll JSON ``POST .../deviceauth/token`` where
  ``403``/``404`` = pending and success returns ``{authorization_code,
  code_verifier, code_challenge}``; finally exchange form
  ``POST {issuer}/oauth/token`` (``grant_type=authorization_code``,
  ``redirect_uri={issuer}/deviceauth/callback``, ``code_verifier``).
- **xAI** -- form ``POST {issuer}/oauth2/device/code`` (``client_id, scope,
  referrer=screenscribe``) -> ``{device_code, user_code, verification_uri,
  verification_uri_complete?, interval?}``; poll form ``POST {issuer}/oauth2/token``
  with ``grant_type=urn:ietf:params:oauth:grant-type:device_code``; errors
  ``authorization_pending`` / ``slow_down`` (+5 s) / ``access_denied`` /
  ``expired_token``. Tokens come straight out of the poll (no PKCE exchange).

``client`` and ``sleep`` are injectable so tests run against
``httpx.MockTransport`` without real waits.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..api_utils import redact_error_message, redact_url
from . import (
    AccountAuthError,
    AccountTokens,
    client_id_for,
    form_body,
    issuer_for,
    provider_oauth_config,
    store_account_tokens,
)

XAI_DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
#: RFC 8628 floor for the xAI poll interval (seconds).
XAI_MIN_INTERVAL_SECONDS = 5
#: Added to the interval each time xAI answers ``slow_down``.
XAI_SLOW_DOWN_INCREMENT_SECONDS = 5
#: Interval used when a device-code response omits one (both providers).
DEFAULT_INTERVAL_SECONDS = 5
#: Whole login budget.
DEFAULT_MAX_WAIT_SECONDS = 15 * 60
#: xAI attribution in the device-code request body.
XAI_REFERRER = "screenscribe"

_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class DeviceCode:
    """What to show the user and what to poll with (provider-agnostic)."""

    provider: str
    verification_url: str
    user_code: str
    #: Codex ``device_auth_id`` or RFC 8628 ``device_code`` -- opaque poll handle.
    device_auth_id: str
    interval: int


def _json_or_error(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise AccountAuthError("oauth", f"{what} returned a non-JSON body") from None
    if not isinstance(body, dict):
        raise AccountAuthError("oauth", f"{what} returned an unexpected body shape")
    return body


def _post(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.post(url, **kwargs)
    except httpx.HTTPError as error:
        detail = redact_error_message(error)
        raise AccountAuthError(
            "http",
            f"request to {redact_url(url)} failed: {error.__class__.__name__}: {detail}",
        ) from None


def _interval(value: Any) -> int:
    """Accept a poll interval as a number or a quoted string (Codex shipped both)."""
    if isinstance(value, bool):
        return DEFAULT_INTERVAL_SECONDS
    if isinstance(value, int | float):
        return int(value) if value > 0 else DEFAULT_INTERVAL_SECONDS
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else DEFAULT_INTERVAL_SECONDS
    return DEFAULT_INTERVAL_SECONDS


# --------------------------------------------------------------------------- #
# Step 1: request a device code
# --------------------------------------------------------------------------- #


def request_device_code(provider: str, *, client: httpx.Client | None = None) -> DeviceCode:
    """Begin a device login; returns the code to display to the user."""
    row = provider_oauth_config(provider)
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        if row.provider == "xai":
            return _request_xai_device_code(http)
        return _request_openai_device_code(http)
    finally:
        if own:
            http.close()


def _request_openai_device_code(client: httpx.Client) -> DeviceCode:
    issuer = issuer_for("openai")
    response = _post(
        client,
        f"{issuer}/api/accounts/deviceauth/usercode",
        json={"client_id": client_id_for("openai")},
    )
    if response.status_code >= 400:
        raise AccountAuthError(
            "oauth", f"device code request failed with status {response.status_code}"
        )
    body = _json_or_error(response, "device code request")
    user_code = body.get("user_code") or body.get("usercode")
    device_auth_id = body.get("device_auth_id")
    if (
        not isinstance(user_code, str)
        or not isinstance(device_auth_id, str)
        or not user_code
        or not device_auth_id
    ):
        raise AccountAuthError(
            "oauth", "device code response is missing user_code / device_auth_id"
        )
    return DeviceCode(
        provider="openai",
        verification_url=f"{issuer}/codex/device",
        user_code=user_code,
        device_auth_id=device_auth_id,
        interval=_interval(body.get("interval")),
    )


def _request_xai_device_code(client: httpx.Client) -> DeviceCode:
    row = provider_oauth_config("xai")
    issuer = issuer_for("xai")
    response = _post(
        client,
        f"{issuer}/oauth2/device/code",
        content=form_body(
            [("client_id", client_id_for("xai")), ("scope", row.scope), ("referrer", XAI_REFERRER)]
        ),
        headers=_FORM_HEADERS,
    )
    if response.status_code >= 400:
        raise AccountAuthError(
            "oauth", f"xAI device code request failed with status {response.status_code}"
        )
    body = _json_or_error(response, "xAI device code request")
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_uri = body.get("verification_uri")
    if not all(isinstance(v, str) and v for v in (device_code, user_code, verification_uri)):
        raise AccountAuthError(
            "oauth",
            "xAI device code response is missing device_code / user_code / verification_uri",
        )
    complete = body.get("verification_uri_complete")
    return DeviceCode(
        provider="xai",
        verification_url=complete
        if isinstance(complete, str) and complete
        else str(verification_uri),
        user_code=str(user_code),
        device_auth_id=str(device_code),
        interval=max(_interval(body.get("interval")), XAI_MIN_INTERVAL_SECONDS),
    )


# --------------------------------------------------------------------------- #
# Step 2: poll until approved, exchange, store
# --------------------------------------------------------------------------- #


def complete_device_code_login(
    device: DeviceCode,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
    on_poll: Callable[[str], None] | None = None,
) -> AccountTokens:
    """Block until the user approves (or the budget runs out), then store tokens.

    ``on_poll`` receives a short status word per round (``pending`` /
    ``slow_down``) for UI feedback; never a token.
    """
    row = provider_oauth_config(device.provider)
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        if row.provider == "xai":
            tokens = _poll_xai(http, device, sleep=sleep, max_wait=max_wait, on_poll=on_poll)
        else:
            tokens = _poll_openai_then_exchange(
                http, device, sleep=sleep, max_wait=max_wait, on_poll=on_poll
            )
    finally:
        if own:
            http.close()
    store_account_tokens(row.provider, tokens)
    return tokens


def _poll_xai(
    client: httpx.Client,
    device: DeviceCode,
    *,
    sleep: Callable[[float], None],
    max_wait: float,
    on_poll: Callable[[str], None] | None,
) -> AccountTokens:
    token_url = f"{issuer_for('xai')}/oauth2/token"
    client_id = client_id_for("xai")
    started = time.monotonic()
    interval = max(device.interval, XAI_MIN_INTERVAL_SECONDS)
    while True:
        response = _post(
            client,
            token_url,
            content=form_body(
                [
                    ("grant_type", XAI_DEVICE_CODE_GRANT_TYPE),
                    ("client_id", client_id),
                    ("device_code", device.device_auth_id),
                ]
            ),
            headers=_FORM_HEADERS,
        )
        if response.status_code < 400:
            return AccountTokens.from_token_response(_json_or_error(response, "xAI token poll"))
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        if not isinstance(error_body, dict):
            error_body = {}
        error = error_body.get("error")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                interval += XAI_SLOW_DOWN_INCREMENT_SECONDS
            if on_poll:
                on_poll(str(error))
            elapsed = time.monotonic() - started
            if elapsed >= max_wait:
                raise AccountAuthError("oauth", "xAI device authorization timed out")
            sleep(min(interval, max_wait - elapsed))
            continue
        if error in ("access_denied", "authorization_denied"):
            raise AccountAuthError("oauth", "xAI device authorization was denied")
        if error == "expired_token":
            raise AccountAuthError(
                "oauth", "xAI device code expired - run `screenscribe auth login xai` again"
            )
        detail = error_body.get("error_description") or error or f"status {response.status_code}"
        raise AccountAuthError("oauth", f"xAI device token exchange failed: {detail}")


def _poll_openai_then_exchange(
    client: httpx.Client,
    device: DeviceCode,
    *,
    sleep: Callable[[float], None],
    max_wait: float,
    on_poll: Callable[[str], None] | None,
) -> AccountTokens:
    issuer = issuer_for("openai")
    client_id = client_id_for("openai")
    poll_url = f"{issuer}/api/accounts/deviceauth/token"
    started = time.monotonic()
    interval = max(device.interval, 1)
    while True:
        response = _post(
            client,
            poll_url,
            json={"device_auth_id": device.device_auth_id, "user_code": device.user_code},
        )
        if response.status_code < 400:
            code_body = _json_or_error(response, "device auth poll")
            break
        if response.status_code in (403, 404):
            if on_poll:
                on_poll("pending")
            elapsed = time.monotonic() - started
            if elapsed >= max_wait:
                raise AccountAuthError("oauth", "device auth timed out")
            sleep(min(interval, max_wait - elapsed))
            continue
        raise AccountAuthError("oauth", f"device auth failed with status {response.status_code}")

    authorization_code = code_body.get("authorization_code")
    code_verifier = code_body.get("code_verifier")
    if not isinstance(authorization_code, str) or not isinstance(code_verifier, str):
        raise AccountAuthError(
            "oauth", "device auth poll returned no authorization_code / code_verifier"
        )
    return exchange_code_for_tokens(
        client,
        issuer=issuer,
        client_id=client_id,
        redirect_uri=f"{issuer}/deviceauth/callback",
        code=authorization_code,
        code_verifier=code_verifier,
    )


def exchange_code_for_tokens(
    client: httpx.Client,
    *,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> AccountTokens:
    """Form ``POST {issuer}/oauth/token`` -- the exact body of the Rust reference."""
    row = provider_oauth_config("openai")
    response = _post(
        client,
        f"{issuer.rstrip('/')}{row.exchange_path}",
        content=form_body(
            [
                ("grant_type", "authorization_code"),
                ("code", code),
                ("redirect_uri", redirect_uri),
                ("client_id", client_id),
                ("code_verifier", code_verifier),
            ]
        ),
        headers=_FORM_HEADERS,
    )
    if response.status_code >= 400:
        raise AccountAuthError("oauth", f"token endpoint returned status {response.status_code}")
    return AccountTokens.from_token_response(_json_or_error(response, "token endpoint"))
