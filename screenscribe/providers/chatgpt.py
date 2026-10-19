"""ChatGPT account (Codex backend) route for LLM and vision Responses calls.

A ChatGPT account token (``screenscribe auth login openai``) is rejected by
``api.openai.com`` but accepted by the ChatGPT Codex backend, the endpoint the
Codex CLI uses. Reference implementation: oh-my-pi
(``packages/catalog/src/wire/codex.ts``,
``packages/ai/src/providers/openai-codex-responses.ts``).

Wire contract, measured live on 2026-10-20 with a screenscribe-stored token:

- ``POST https://chatgpt.com/backend-api/codex/responses`` speaks the Responses
  API with ``Authorization: Bearer <account access token>`` plus
  ``chatgpt-account-id`` (JWT claim ``https://api.openai.com/auth``
  ``.chatgpt_account_id``), ``originator``, ``version`` and
  ``OpenAI-Beta: responses=experimental``.
- ``store`` must be ``false`` and ``stream`` must be ``true`` (HTTP 400
  otherwise); ``temperature`` is rejected. ``instructions`` is optional,
  ``reasoning.effort`` (``none`` .. ``xhigh``) and ``input_image`` data URLs work.
- ``GET https://chatgpt.com/backend-api/codex/models?client_version=<version>``
  lists the models this account may use; ``version`` gates newer models.

Every screenscribe LLM/vision client is built through
:func:`screenscribe.api_utils.provider_client_options`, which mounts
:class:`CodexTransport` for this endpoint. The transport adapts the body, adds
the headers, answers non-streaming callers with the aggregated final response
JSON (the ``response.completed`` event) and, on HTTP 401, refreshes the account
once (:func:`screenscribe.account_auth.force_refresh`) and retries. No caller
needs to know it is talking to ChatGPT.
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.request
import uuid
from typing import Any

import httpx

CHATGPT_HOST = "chatgpt.com"
CHATGPT_CODEX_BASE = f"https://{CHATGPT_HOST}/backend-api/codex"
CHATGPT_RESPONSES_ENDPOINT = f"{CHATGPT_CODEX_BASE}/responses"
CHATGPT_MODELS_ENDPOINT = f"{CHATGPT_CODEX_BASE}/models"
#: Codex client version the backend gates model availability on (oh-my-pi pins
#: the same value; an older one hides newer models).
CODEX_CLIENT_VERSION = "0.159.0"
ORIGINATOR = "screenscribe"
JWT_AUTH_CLAIM = "https://api.openai.com/auth"
#: Request fields the backend refuses (HTTP 400 "Unsupported parameter").
UNSUPPORTED_FIELDS = ("temperature", "top_p")
#: Marks a response this transport assembled from the event stream.
AGGREGATED_HEADER = "x-screenscribe-route"
AGGREGATED_VALUE = "chatgpt-codex-aggregated"
#: Events that end a Responses stream with the full response object.
_FINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed")


def is_chatgpt_codex_endpoint(url: str | httpx.URL) -> bool:
    """True for ``https://chatgpt.com/backend-api/codex/...`` only."""
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and (parsed.host or "").lower() == CHATGPT_HOST
        and parsed.path.startswith("/backend-api/codex/")
    )


def account_id_from_token(access_token: str) -> str | None:
    """``chatgpt_account_id`` from the (unverified) access-token JWT."""
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    auth = claims.get(JWT_AUTH_CLAIM) if isinstance(claims, dict) else None
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account_id if isinstance(account_id, str) and account_id else None


def _bearer(headers: httpx.Headers) -> str:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def adapt_body(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Codex-backend body plus whether the caller itself asked for a stream.

    The backend keeps no state (``store: false``), so ``previous_response_id``
    cannot chain and is dropped; callers already refuse to chain on this host.
    """
    adapted = {
        key: value
        for key, value in body.items()
        if key not in UNSUPPORTED_FIELDS and key != "previous_response_id"
    }
    caller_streams = bool(body.get("stream"))
    adapted["store"] = False
    adapted["stream"] = True
    return adapted, caller_streams


def codex_headers(source: httpx.Headers, access_token: str, session_id: str) -> httpx.Headers:
    """Original headers plus the Codex identity headers; Content-Length recomputed.

    Assignment on :class:`httpx.Headers` is case-insensitive, so a caller's
    ``authorization``/``content-type`` is replaced, never duplicated (Cloudflare
    in front of the backend answers duplicated headers with a bare 400).
    """
    from .. import __version__

    headers = httpx.Headers(source)
    for name in ("content-length", "host"):
        if name in headers:
            del headers[name]
    headers["Authorization"] = f"Bearer {access_token}"
    account_id = account_id_from_token(access_token)
    if account_id:
        headers["chatgpt-account-id"] = account_id
    headers["originator"] = ORIGINATOR
    headers["version"] = CODEX_CLIENT_VERSION
    headers["OpenAI-Beta"] = "responses=experimental"
    headers["session_id"] = session_id
    headers["User-Agent"] = f"screenscribe/{__version__}"
    headers["Accept"] = "text/event-stream"
    headers["Content-Type"] = "application/json"
    return headers


def aggregate_event_stream(raw: bytes) -> dict[str, Any]:
    """Final response object from a Responses SSE body.

    ``response.completed`` / ``incomplete`` / ``failed`` carry the whole
    response; an ``error`` event or a stream that ends without a final event
    becomes ``status: failed`` so callers raise their usual payload error.
    Output items seen in ``response.output_item.done`` fill an empty
    ``output`` list.
    """
    final: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            items.append(event["item"])
        elif kind in _FINAL_EVENTS and isinstance(event.get("response"), dict):
            final = event["response"]
        elif kind == "error":
            nested = event.get("error")
            detail: dict[str, Any] = nested if isinstance(nested, dict) else event
            error = {
                "code": detail.get("code") or "stream_error",
                "message": detail.get("message") or "the ChatGPT stream reported an error",
            }
    if final is None:
        return {
            "status": "failed",
            "output": items,
            "error": error
            or {"code": "stream_incomplete", "message": "stream ended without a final response"},
        }
    if not final.get("output") and items:
        final = {**final, "output": items}
    return final


def _aggregated_response(raw: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", AGGREGATED_HEADER: AGGREGATED_VALUE},
        content=json.dumps(aggregate_event_stream(raw)).encode("utf-8"),
    )


class _CodexAdapter:
    """Request rewriting shared by the sync and async transports."""

    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())

    def rewrite(
        self, request: httpx.Request, access_token: str | None = None
    ) -> tuple[httpx.Request, bool] | None:
        """Adapted request and the caller's stream flag; None = pass through."""
        if request.method != "POST" or not is_chatgpt_codex_endpoint(request.url):
            return None
        try:
            body = json.loads(request.content or b"{}")
        except (ValueError, httpx.RequestNotRead):
            return None
        if not isinstance(body, dict):
            return None
        adapted, caller_streams = adapt_body(body)
        token = access_token or _bearer(request.headers)
        rewritten = httpx.Request(
            request.method,
            request.url,
            headers=codex_headers(request.headers, token, self.session_id),
            content=json.dumps(adapted).encode("utf-8"),
            extensions=request.extensions,
        )
        return rewritten, caller_streams


def _refreshed_token(rejected: str) -> str | None:
    """A replacement for a ChatGPT account token the backend answered 401 to, or None.

    Only a bearer that is itself a ChatGPT account token (a JWT carrying
    ``chatgpt_account_id``) is refreshed. Any other bearer -- an API key a
    caller routed here on purpose -- gets its 401 back untouched: the stored
    account token is never swapped in for a credential the caller chose.
    """
    from .. import account_auth

    if not rejected or account_id_from_token(rejected) is None:
        return None
    try:
        fresh = account_auth.force_refresh("openai", rejected).access_token
    except (account_auth.AccountAuthError, OSError):
        return None
    return fresh if fresh != rejected else None


def _environment_proxy() -> str | None:
    """The proxy httpx's own environment routing would pick for chatgpt.com.

    The adapting transport is mounted on the ChatGPT origin, and a mount takes
    precedence over the client's ``HTTPS_PROXY``/``ALL_PROXY`` routing, so the
    inner connection re-applies it (``NO_PROXY`` included) -- the same
    ``urllib`` source httpx reads.
    """
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if not proxy or urllib.request.proxy_bypass(CHATGPT_HOST):
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


class CodexTransport(httpx.BaseTransport):
    """Sync transport for ``httpx.Client`` aimed at the Codex backend."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner or httpx.HTTPTransport(proxy=_environment_proxy())
        self._adapter = _CodexAdapter()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        prepared = self._adapter.rewrite(request)
        if prepared is None:
            return self._inner.handle_request(request)
        rewritten, caller_streams = prepared
        response = self._inner.handle_request(rewritten)
        if response.status_code == 401:
            try:
                fresh = _refreshed_token(_bearer(rewritten.headers))
            except BaseException:
                response.close()
                raise
            retry = self._adapter.rewrite(request, fresh) if fresh else None
            if retry is not None:
                response.close()
                response = self._inner.handle_request(retry[0])
        if caller_streams or response.status_code >= 300:
            return response
        try:
            raw = response.read()
        finally:
            response.close()
        return _aggregated_response(raw)

    def close(self) -> None:
        self._inner.close()


class AsyncCodexTransport(httpx.AsyncBaseTransport):
    """Async twin of :class:`CodexTransport` for ``httpx.AsyncClient``."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(proxy=_environment_proxy())
        self._adapter = _CodexAdapter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        prepared = self._adapter.rewrite(request)
        if prepared is None:
            return await self._inner.handle_async_request(request)
        rewritten, caller_streams = prepared
        response = await self._inner.handle_async_request(rewritten)
        if response.status_code == 401:
            # The account store lock and the token request block: off the loop.
            try:
                fresh = await asyncio.to_thread(_refreshed_token, _bearer(rewritten.headers))
            except BaseException:
                await response.aclose()
                raise
            retry = self._adapter.rewrite(request, fresh) if fresh else None
            if retry is not None:
                await response.aclose()
                response = await self._inner.handle_async_request(retry[0])
        if caller_streams or response.status_code >= 300:
            return response
        try:
            raw = await response.aread()
        finally:
            await response.aclose()
        return _aggregated_response(raw)

    async def aclose(self) -> None:
        await self._inner.aclose()


def list_models(access_token: str, *, client: httpx.Client | None = None) -> list[str]:
    """Model slugs this ChatGPT account may use on the Codex backend."""
    own = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=False)
    try:
        response = http.get(
            CHATGPT_MODELS_ENDPOINT,
            params={"client_version": CODEX_CLIENT_VERSION},
            headers={
                k: v
                for k, v in codex_headers(httpx.Headers(), access_token, str(uuid.uuid4())).items()
                if k not in ("accept", "content-type")
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if own:
            http.close()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [m["slug"] for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)]
