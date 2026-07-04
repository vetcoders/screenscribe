"""Pytest defaults for fast local iteration."""

from __future__ import annotations

import os

import pytest
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Item

# Force color OFF for the whole test session. CI (Rich terminal detection /
# FORCE_COLOR) otherwise emits ANSI escape codes that get embedded mid-phrase in
# captured CLI output, breaking substring asserts that pass locally (no color).
# NO_COLOR is the standard kill switch; pop FORCE_COLOR so it cannot override it.
os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"


def pytest_addoption(parser: Parser) -> None:
    """Add explicit switches for tests that hit live providers or a real browser."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require configured external APIs.",
    )
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests (build+install wheel, real browser via Playwright).",
    )


def pytest_collection_modifyitems(config: Config, items: list[Item]) -> None:
    """Skip integration / e2e tests unless the caller opted in explicitly."""
    run_integration = config.getoption("--run-integration")
    run_e2e = config.getoption("--run-e2e")

    skip_integration = pytest.mark.skip(
        reason="integration tests are skipped by default; pass --run-integration to include them",
    )
    skip_e2e = pytest.mark.skip(
        reason="e2e tests are skipped by default; pass --run-e2e to include them",
    )
    for item in items:
        if not run_integration and "integration" in item.keywords:
            item.add_marker(skip_integration)
        if not run_e2e and "e2e" in item.keywords:
            item.add_marker(skip_e2e)


# --- Make the test client speak to the security-guarded servers -------------
# The analyze/review apps gate /api/* behind a localhost Host + a per-process
# session token (screenscribe.server_security). Configure Starlette's TestClient
# to (1) present a localhost Host and (2) auto-attach the app's session token on
# /api/ calls, so the whole existing suite exercises the real guarded app
# unchanged. Tests that assert the rejection paths pass their own Host / Origin /
# token explicitly, and those values win — we only fill in what the caller omits.
from starlette.testclient import TestClient as _TestClient  # noqa: E402

_orig_tc_init = _TestClient.__init__
_orig_tc_request = _TestClient.request


def _tc_init(self, app, *args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("base_url", "http://127.0.0.1")
    _orig_tc_init(self, app, *args, **kwargs)


def _tc_request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
    app = getattr(self, "app", None)
    token = getattr(getattr(app, "state", None), "session_token", None)
    if token and "/api/" in str(url):
        headers = dict(kwargs.get("headers") or {})
        if not any(k.lower() == "x-screenscribe-token" for k in headers):
            headers["X-ScreenScribe-Token"] = token
            kwargs["headers"] = headers
    return _orig_tc_request(self, method, url, *args, **kwargs)


_TestClient.__init__ = _tc_init
_TestClient.request = _tc_request
