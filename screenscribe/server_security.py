"""Localhost-only security guard for the analyze / review dev servers.

These servers expose endpoints that spend API credits (STT / LLM / VLM) and read
the user's media. They bind to 127.0.0.1, but ``127.0.0.1`` alone is not enough:
a malicious web page open in the user's browser could try to drive these
endpoints (CSRF / DNS-rebinding). This module adds three cheap, layered checks
that a cross-site page cannot satisfy:

* **Host guard (all paths)** — the ``Host`` header must resolve to localhost.
  Blocks DNS-rebinding: an attacker domain pointed at 127.0.0.1 still sends its
  own host name.
* **Origin guard (``/api/*``)** — browser requests must carry a localhost
  ``Origin`` (or none at all, for same-origin navigations / non-browser clients).
* **Session token (``/api/*``)** — a per-process random token handed to the UI
  through the URL fragment (``#token=...``) and echoed back as the
  ``X-ScreenScribe-Token`` header. A cross-site page cannot read another page's
  fragment, so it cannot forge the header.

The fragment is never sent to the server, so it stays out of access logs and the
``Referer`` header; the UI reads it from ``location.hash`` and strips it.

One narrow exception to the header requirement: ``GET /api/marker/{id}/frame``.
The dashboard renders marker thumbnails as plain ``<img src>`` elements and the
browser cannot attach a custom header to an image request. Those URLs instead
carry a per-marker signature in the ``st`` query parameter — an HMAC-SHA256 of
the marker id keyed by the session token (see :func:`frame_access_token`). A
cross-site page cannot compute it without the session token, so the endpoint
stays as unforgeable as the header path while remaining ``<img>``-loadable.
Every other ``/api/*`` request (and any non-GET on the frame path) still
requires the header.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_LOCALHOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Request header the UI echoes the session token back on. Inlined where used so
# static scanners don't mistake a "*_TOKEN" constant name for a hardcoded secret.


def generate_session_token() -> str:
    """Return a fresh URL-safe per-process session token."""
    return secrets.token_urlsafe(32)


def tokenized_url(base_url: str, token: str) -> str:
    """Build the UI URL carrying the session token in the (server-invisible)
    fragment, e.g. ``http://localhost:8766#token=...``."""
    return f"{base_url}#token={token}"


# GET on exactly this path may authenticate via the signed ``st`` query
# parameter instead of the header (an ``<img src>`` cannot send headers).
_FRAME_PATH = re.compile(r"^/api/marker/([^/]+)/frame$")


def frame_access_token(session_token: str, marker_id: str) -> str:
    """Derive the per-marker signature carried by frame ``<img>`` URLs.

    HMAC-SHA256 keyed by the per-process session token over the marker id:
    valid only for this process, only for this marker, and only useful on the
    GET frame path the guard scopes it to. Knowing one signature reveals
    nothing about the session token or other markers' signatures.
    """
    return hmac.new(
        session_token.encode(), f"frame:{marker_id}".encode(), hashlib.sha256
    ).hexdigest()


def _frame_query_token_ok(request: Request, token: str) -> bool:
    """True only for ``GET /api/marker/{id}/frame`` carrying a valid ``st``
    signature for that exact marker id."""
    if request.method != "GET":
        return False
    match = _FRAME_PATH.match(request.url.path)
    if not match:
        return False
    supplied = request.query_params.get("st", "")
    expected = frame_access_token(token, match.group(1))
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _normalize_host(host: str) -> str:
    """Case-fold and strip the trailing FQDN dot — ``LOCALHOST`` and
    ``localhost.`` are the same host as ``localhost``."""
    return host.strip().lower().rstrip(".")


def _header_host(raw: str) -> str:
    """Extract the bare normalized hostname from a Host header value (strips
    port + IPv6 brackets, case, trailing dot)."""
    raw = (raw or "").strip()
    if raw.startswith("["):  # [::1] or [::1]:port
        return _normalize_host(raw[1:].split("]", 1)[0])
    return _normalize_host(raw.split(":", 1)[0])


def _host_is_local(request: Request) -> bool:
    return _header_host(request.headers.get("host", "")) in _LOCALHOSTS


def _origin_is_local(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        # No Origin header: same-origin navigation or a non-browser client.
        return True
    return _normalize_host(urlparse(origin).hostname or "") in _LOCALHOSTS


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def install_security(app: FastAPI, token: str) -> None:
    """Attach the localhost guard middleware and record the token on app.state."""
    app.state.session_token = token

    @app.middleware("http")
    async def _guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not _host_is_local(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden: non-local Host"})
        if _is_api_path(request.url.path):
            if not _origin_is_local(request):
                return JSONResponse(
                    status_code=403, content={"detail": "Forbidden: cross-origin request"}
                )
            supplied = request.headers.get("X-ScreenScribe-Token", "")
            header_ok = bool(supplied) and secrets.compare_digest(supplied, token)
            if not (header_ok or _frame_query_token_ok(request, token)):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden: missing or invalid session token"},
                )
        return await call_next(request)
