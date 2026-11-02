"""ChatGPT account route (Codex backend): transport, client wiring, config sovereignty.

Every HTTP call goes through ``httpx.MockTransport`` -- no network. ``Path.home``
is redirected to ``tmp_path`` so the real ``~/.config/screenscribe/accounts.json``
and ``config.env`` are never read or written.
"""

# ruff: noqa: S105 -- fixture tokens are literal test strings, not credentials
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from screenscribe import __version__, account_auth
from screenscribe.account_auth import (
    AccountAuthError,
    AccountTokens,
    load_account_tokens,
    store_account_tokens,
)
from screenscribe.agent.chat import AgentProvider, _supports_stateful_response_chain
from screenscribe.api_utils import (
    UnsafeEndpointError,
    extract_llm_response_text,
    extract_response_payload_error,
    provider_client_options,
)
from screenscribe.config import (
    CHATGPT_SIGN_IN_HINT,
    LIBRAXIS_LLM_ENDPOINT,
    LIBRAXIS_STT_ENDPOINT,
    OPENAI_LLM_ENDPOINT,
    OPENAI_STT_ENDPOINT,
    XAI_STT_ENDPOINT,
    ScreenScribeConfig,
)
from screenscribe.providers.chatgpt import (
    AGGREGATED_HEADER,
    AGGREGATED_VALUE,
    CHATGPT_MODELS_ENDPOINT,
    CHATGPT_RESPONSES_ENDPOINT,
    CODEX_CLIENT_VERSION,
    JWT_AUTH_CLAIM,
    ORIGINATOR,
    AsyncCodexTransport,
    CodexTransport,
    account_id_from_token,
    adapt_body,
    aggregate_event_stream,
    codex_headers,
    is_chatgpt_codex_endpoint,
    list_models,
)
from screenscribe.transcript_sources import response_id_can_chain
from screenscribe.unified.analyze_one import _may_chain_previous_response

CODEX_URL = CHATGPT_RESPONSES_ENDPOINT
OPENAI_ISSUER = "https://auth.openai.test"
ACCOUNT_ID = "acct-fixture-1"
REFRESHED_ACCOUNT_ID = "acct-fixture-2"
SK_OPENAI = "sk-proj-fixture-openai"  # pragma: allowlist secret
LIBRAXIS_KEY = "test-libraxis-key"  # pragma: allowlist secret
SK_GENERIC = "sk-fixture-generic"  # pragma: allowlist secret
XAI_KEY = "xai-fixture-key"  # pragma: allowlist secret
REFRESH_TOKEN = "rt-fixture"  # pragma: allowlist secret


def _jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def _account_token(account_id: str = ACCOUNT_ID, *, nonce: str = "a") -> str:
    return _jwt({JWT_AUTH_CLAIM: {"chatgpt_account_id": account_id}, "nonce": nonce})


TOKEN = _account_token()
FRESH_TOKEN = _account_token(REFRESHED_ACCOUNT_ID, nonce="b")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SCREENSCRIBE_OPENAI_OAUTH_ISSUER", OPENAI_ISSUER)
    monkeypatch.delenv("SCREENSCRIBE_OPENAI_OAUTH_CLIENT_ID", raising=False)
    ScreenScribeConfig._account_warned.clear()


# --------------------------------------------------------------------------- #
# Helpers: SSE bodies, scripted upstream
# --------------------------------------------------------------------------- #


def _message(text: str, item_id: str = "msg_1") -> dict[str, Any]:
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _response(status: str = "completed", output: list[Any] | None = None, **extra: Any) -> dict:
    return {
        "id": "resp_fixture",
        "object": "response",
        "status": status,
        "model": "gpt-5.6-luna",
        "output": [_message("hello from codex")] if output is None else output,
        **extra,
    }


def _sse(*events: dict[str, Any], tail: str = "") -> bytes:
    chunks = [f"event: {e.get('type', 'message')}\ndata: {json.dumps(e)}\n\n" for e in events]
    return ("".join(chunks) + tail).encode()


def _completed_stream(final: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]]:
    final = final or _response()
    body = _sse(
        {"type": "response.created", "response": {**final, "status": "in_progress", "output": []}},
        {"type": "response.output_text.delta", "delta": "hello "},
        {"type": "response.output_item.done", "item": _message("hello from codex")},
        {"type": "response.completed", "response": final},
        tail=": keepalive\n\ndata: [DONE]\n\n",
    )
    return body, final


def _sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


class Upstream:
    """MockTransport handler: scripted responses in order + call log."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.calls: list[httpx.Request] = []
        self.queue = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.queue:
            return httpx.Response(599, json={"error": "unscripted"})
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.calls[index].content)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=CodexTransport(inner=httpx.MockTransport(self)))

    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=AsyncCodexTransport(inner=httpx.MockTransport(self)))


def _caller_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "gpt-5.6-luna",
        "instructions": "Be terse.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "reasoning": {"summary": "auto", "effort": "none"},
        "tools": [{"type": "function", "name": "noop", "parameters": {"type": "object"}}],
        "temperature": 0.2,
        "top_p": 0.9,
        "previous_response_id": "resp_previous",
        "store": True,
    }
    body.update(overrides)
    return body


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class RefreshRecorder:
    """Stand-in for ``account_auth.force_refresh``."""

    def __init__(self, result: str | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def __call__(self, provider: str, rejected: str, **_: Any) -> AccountTokens:
        self.calls.append((provider, rejected))
        if isinstance(self.result, Exception):
            raise self.result
        return AccountTokens(access_token=self.result)


# --------------------------------------------------------------------------- #
# Endpoint recognition + JWT claim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CHATGPT_RESPONSES_ENDPOINT, True),
        (CHATGPT_MODELS_ENDPOINT, True),
        ("https://CHATGPT.COM/backend-api/codex/responses", True),
        ("http://chatgpt.com/backend-api/codex/responses", False),
        ("https://chatgpt.com.evil.test/backend-api/codex/responses", False),
        ("https://api.chatgpt.com/backend-api/codex/responses", False),
        ("https://chatgpt.com/backend-api/codex", False),
        ("https://chatgpt.com/backend-api/codexx/responses", False),
        ("https://chatgpt.com/backend-api/conversation", False),
        (OPENAI_LLM_ENDPOINT, False),
        (LIBRAXIS_LLM_ENDPOINT, False),
        ("not a url", False),
        ("", False),
    ],
)
def test_is_chatgpt_codex_endpoint(url: str, expected: bool) -> None:
    assert is_chatgpt_codex_endpoint(url) is expected


def test_account_id_from_token_reads_the_auth_claim() -> None:
    assert account_id_from_token(TOKEN) == ACCOUNT_ID


@pytest.mark.parametrize(
    "token",
    [
        SK_OPENAI,
        "a.b",
        "header.!!!not-base64!!!.sig",
        _jwt({"sub": "user"}),
        _jwt({JWT_AUTH_CLAIM: "not-a-dict"}),
        _jwt({JWT_AUTH_CLAIM: {"chatgpt_account_id": 42}}),
        _jwt({JWT_AUTH_CLAIM: {"chatgpt_account_id": ""}}),
        "header." + base64.urlsafe_b64encode(b"[1, 2]").decode().rstrip("=") + ".sig",
    ],
)
def test_account_id_from_token_rejects_malformed_tokens(token: str) -> None:
    assert account_id_from_token(token) is None


# --------------------------------------------------------------------------- #
# 1. Body adaptation
# --------------------------------------------------------------------------- #


def test_adapt_body_forces_store_false_stream_true_and_drops_unsupported_fields() -> None:
    original = _caller_body()
    snapshot = json.loads(json.dumps(original))

    adapted, caller_streams = adapt_body(original)

    assert caller_streams is False
    assert adapted["store"] is False
    assert adapted["stream"] is True
    for dropped in ("temperature", "top_p", "previous_response_id"):
        assert dropped not in adapted
    for kept in ("model", "input", "reasoning", "tools", "instructions"):
        assert adapted[kept] == original[kept]
    assert original == snapshot, "adapt_body must not mutate the caller's body"


@pytest.mark.parametrize(("stream", "expected"), [(True, True), (False, False), (None, False)])
def test_adapt_body_reports_the_callers_stream_flag(stream: bool | None, expected: bool) -> None:
    body = _caller_body() if stream is None else _caller_body(stream=stream)
    if stream is None:
        body.pop("stream", None)
    adapted, caller_streams = adapt_body(body)
    assert caller_streams is expected
    assert adapted["stream"] is True


def test_transport_sends_the_adapted_body_upstream() -> None:
    sse, _ = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    with upstream.client() as client:
        client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    sent = upstream.body()
    assert sent["store"] is False
    assert sent["stream"] is True
    assert not {"temperature", "top_p", "previous_response_id"} & sent.keys()
    expected = _caller_body()
    for kept in ("model", "input", "reasoning", "tools", "instructions"):
        assert sent[kept] == expected[kept]


# --------------------------------------------------------------------------- #
# 2. Headers
# --------------------------------------------------------------------------- #


def test_transport_headers_replace_caller_auth_and_add_codex_identity() -> None:
    sse, _ = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    caller_headers = {
        "authorization": f"Bearer {TOKEN}",
        "content-type": "application/json",
        "x-caller": "kept",
    }
    with upstream.client() as client:
        client.post(CODEX_URL, content=json.dumps(_caller_body()), headers=caller_headers)

    request = upstream.calls[0]
    headers = request.headers
    assert headers.get_list("authorization") == [f"Bearer {TOKEN}"]
    assert headers.get_list("content-type") == ["application/json"]
    assert headers.get_list("chatgpt-account-id") == [ACCOUNT_ID]
    assert headers.get_list("originator") == [ORIGINATOR] == ["screenscribe"]
    assert headers.get_list("version") == [CODEX_CLIENT_VERSION]
    assert headers.get_list("openai-beta") == ["responses=experimental"]
    assert headers.get_list("accept") == ["text/event-stream"]
    assert headers.get_list("user-agent") == [f"screenscribe/{__version__}"]
    assert headers.get_list("host") == ["chatgpt.com"]
    assert headers["x-caller"] == "kept"
    session_ids = headers.get_list("session_id")
    assert len(session_ids) == 1
    uuid.UUID(session_ids[0])
    # The adapted body is longer than the caller's: a stale length would truncate it.
    assert headers.get_list("content-length") == [str(len(request.content))]


def test_session_id_is_stable_per_transport_and_unique_across_transports() -> None:
    sse, _ = _completed_stream()
    first, second = Upstream(_sse_response(sse)), Upstream(_sse_response(sse))
    with first.client() as client:
        client.post(CODEX_URL, json=_caller_body(), headers=_auth())
        client.post(CODEX_URL, json=_caller_body(), headers=_auth())
    with second.client() as client:
        client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    a, b = (c.headers["session_id"] for c in first.calls)
    assert a == b
    assert second.calls[0].headers["session_id"] != a


def test_codex_headers_collapse_duplicated_caller_headers() -> None:
    source = httpx.Headers(
        [
            ("authorization", "Bearer stale-one"),
            ("Authorization", "Bearer stale-two"),
            ("content-type", "text/plain"),
            ("Content-Type", "application/x-stale"),
            ("content-length", "9999"),
            ("host", "elsewhere.test"),
        ]
    )
    headers = codex_headers(source, TOKEN, "session-fixture")

    assert headers.get_list("authorization") == [f"Bearer {TOKEN}"]
    assert headers.get_list("content-type") == ["application/json"]
    assert "content-length" not in headers
    assert "host" not in headers
    assert headers["session_id"] == "session-fixture"


def test_codex_headers_omit_account_id_for_a_token_without_the_claim() -> None:
    headers = codex_headers(httpx.Headers(), "opaque-token", "session-fixture")
    assert "chatgpt-account-id" not in headers
    assert headers["authorization"] == "Bearer opaque-token"


# --------------------------------------------------------------------------- #
# 3. Non-streaming caller: one aggregated JSON response
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stream_field", ["absent", "false"])
def test_non_streaming_caller_gets_the_completed_response_as_json(stream_field: str) -> None:
    sse, final = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    body = _caller_body() if stream_field == "absent" else _caller_body(stream=False)
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=body, headers=_auth())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers[AGGREGATED_HEADER] == AGGREGATED_VALUE == "chatgpt-codex-aggregated"
    payload = response.json()
    assert payload == final
    assert extract_llm_response_text(payload, CODEX_URL) == "hello from codex"
    assert extract_response_payload_error(payload) is None
    assert len(upstream.calls) == 1


# --------------------------------------------------------------------------- #
# 4. Failures inside the stream become payload errors
# --------------------------------------------------------------------------- #


def _aggregate_via_transport(sse: bytes) -> dict[str, Any]:
    upstream = Upstream(_sse_response(sse))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(), headers=_auth())
    assert response.status_code == 200
    assert response.headers[AGGREGATED_HEADER] == AGGREGATED_VALUE
    return response.json()


def test_response_failed_event_maps_to_a_failed_payload_error() -> None:
    failed = _response(
        "failed", output=[], error={"code": "server_error", "message": "The model crashed"}
    )
    payload = _aggregate_via_transport(_sse({"type": "response.failed", "response": failed}))

    assert payload["status"] == "failed"
    error = extract_response_payload_error(payload)
    assert error is not None
    assert "The model crashed" in str(error)
    assert error.code == "server_error"


def test_response_incomplete_event_maps_to_an_incomplete_payload_error() -> None:
    incomplete = _response(
        "incomplete", output=[], incomplete_details={"reason": "max_output_tokens"}
    )
    payload = _aggregate_via_transport(
        _sse(
            {"type": "response.output_item.done", "item": _message("partial")},
            {"type": "response.incomplete", "response": incomplete},
        )
    )

    assert payload["status"] == "incomplete"
    error = extract_response_payload_error(payload)
    assert error is not None
    assert "max_output_tokens" in str(error)


@pytest.mark.parametrize(
    ("event", "code", "message"),
    [
        (
            {"type": "error", "error": {"code": "rate_limit_exceeded", "message": "Slow down"}},
            "rate_limit_exceeded",
            "Slow down",
        ),
        (
            {"type": "error", "code": "server_error", "message": "Upstream fault"},
            "server_error",
            "Upstream fault",
        ),
        ({"type": "error"}, "stream_error", "the ChatGPT stream reported an error"),
    ],
)
def test_error_event_without_final_maps_to_a_failed_payload(
    event: dict[str, Any], code: str, message: str
) -> None:
    payload = _aggregate_via_transport(
        _sse({"type": "response.output_text.delta", "delta": "par"}, event)
    )

    assert payload["status"] == "failed"
    assert payload["error"] == {"code": code, "message": message}
    error = extract_response_payload_error(payload)
    assert error is not None
    assert message in str(error)
    assert error.code == code


def test_stream_ending_without_a_final_event_is_a_failure() -> None:
    payload = _aggregate_via_transport(
        _sse(
            {"type": "response.created", "response": _response("in_progress", output=[])},
            {"type": "response.output_text.delta", "delta": "cut off"},
        )
    )

    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "stream_incomplete"
    error = extract_response_payload_error(payload)
    assert error is not None
    assert error.code == "stream_incomplete"


def test_empty_stream_body_is_a_failure_not_a_crash() -> None:
    payload = aggregate_event_stream(b"")
    assert payload["status"] == "failed"
    assert payload["output"] == []
    assert extract_response_payload_error(payload) is not None


def test_aggregation_ignores_comments_done_sentinel_and_garbage_lines() -> None:
    final = _response()
    raw = (
        b": keepalive\r\n\r\n"
        b"data: not-json\r\n\r\n"
        b"data: [1, 2, 3]\r\n\r\n"
        b"data:\r\n\r\n"
        + _sse({"type": "response.completed", "response": final}).replace(b"\n", b"\r\n")
        + b"data: [DONE]\r\n\r\n"
    )
    assert aggregate_event_stream(raw) == final


# --------------------------------------------------------------------------- #
# 5. Empty final output is filled from output_item.done
# --------------------------------------------------------------------------- #


def test_empty_final_output_is_filled_from_output_item_done_events() -> None:
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
    message = _message("assembled answer", item_id="msg_9")
    final = _response(output=[])
    payload = _aggregate_via_transport(
        _sse(
            {"type": "response.output_item.done", "item": reasoning},
            {"type": "response.output_item.done", "item": message},
            {"type": "response.output_item.done", "item": "not-a-dict"},
            {"type": "response.completed", "response": final},
        )
    )

    assert payload["output"] == [reasoning, message]
    assert payload["id"] == final["id"]
    assert extract_llm_response_text(payload, CODEX_URL) == "assembled answer"


def test_non_empty_final_output_wins_over_streamed_items() -> None:
    final = _response(output=[_message("final copy")])
    payload = aggregate_event_stream(
        _sse(
            {"type": "response.output_item.done", "item": _message("streamed copy")},
            {"type": "response.completed", "response": final},
        )
    )
    assert payload == final


# --------------------------------------------------------------------------- #
# 6. Streaming caller: raw SSE passes through
# --------------------------------------------------------------------------- #


def test_streaming_caller_gets_the_raw_event_stream_unchanged() -> None:
    sse, _ = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(stream=True), headers=_auth())

    assert response.status_code == 200
    assert response.content == sse
    assert response.headers["content-type"] == "text/event-stream"
    assert AGGREGATED_HEADER not in response.headers
    assert upstream.body()["store"] is False


def test_streaming_caller_can_iterate_the_event_stream() -> None:
    sse, _ = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    with (
        upstream.client() as client,
        client.stream(
            "POST", CODEX_URL, json=_caller_body(stream=True), headers=_auth()
        ) as response,
    ):
        lines = list(response.iter_lines())
    assert any('"response.completed"' in line for line in lines)


# --------------------------------------------------------------------------- #
# 7. HTTP error statuses pass through unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
@pytest.mark.parametrize("stream", [False, True])
def test_http_error_statuses_pass_through(
    status: int, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh = RefreshRecorder(FRESH_TOKEN)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    error_body = {"error": {"message": f"status {status}", "type": "fixture"}}
    upstream = Upstream(httpx.Response(status, json=error_body, headers={"retry-after": "7"}))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(stream=stream), headers=_auth())

    assert response.status_code == status
    assert response.json() == error_body
    assert response.headers["retry-after"] == "7"
    assert AGGREGATED_HEADER not in response.headers
    assert len(upstream.calls) == 1
    assert refresh.calls == []


# --------------------------------------------------------------------------- #
# 8. 401 -> refresh once -> retry
# --------------------------------------------------------------------------- #


def test_401_refreshes_once_and_retries_with_the_new_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh = RefreshRecorder(FRESH_TOKEN)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    sse, final = _completed_stream()
    upstream = Upstream(httpx.Response(401, json={"error": "expired"}), _sse_response(sse))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    assert response.status_code == 200
    assert response.json() == final
    assert refresh.calls == [("openai", TOKEN)]
    assert len(upstream.calls) == 2
    first, retry = upstream.calls
    assert first.headers.get_list("authorization") == [f"Bearer {TOKEN}"]
    assert retry.headers.get_list("authorization") == [f"Bearer {FRESH_TOKEN}"]
    assert retry.headers["chatgpt-account-id"] == REFRESHED_ACCOUNT_ID
    assert retry.headers["session_id"] == first.headers["session_id"]
    assert json.loads(retry.content) == json.loads(first.content)
    assert retry.headers.get_list("content-length") == [str(len(retry.content))]


@pytest.mark.parametrize(
    "outcome",
    [AccountAuthError("revoked", "refresh refused: invalid_grant"), TOKEN],
    ids=["refresh-raises", "same-token"],
)
def test_401_without_a_new_token_returns_the_original_401(
    outcome: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh = RefreshRecorder(outcome)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    upstream = Upstream(httpx.Response(401, json={"error": "expired"}))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    assert response.status_code == 401
    assert response.json() == {"error": "expired"}
    assert refresh.calls == [("openai", TOKEN)]
    assert len(upstream.calls) == 1


def test_401_without_a_caller_bearer_never_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh = RefreshRecorder(FRESH_TOKEN)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    upstream = Upstream(httpx.Response(401, json={"error": "no auth"}))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body())

    assert response.status_code == 401
    assert refresh.calls == []
    assert len(upstream.calls) == 1


def test_401_after_the_retry_is_returned_without_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh = RefreshRecorder(FRESH_TOKEN)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    upstream = Upstream(httpx.Response(401, json={"error": "still no"}))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    assert response.status_code == 401
    assert len(refresh.calls) == 1
    assert len(upstream.calls) == 2


def _oauth_client(*responses: httpx.Response) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_401_retry_spends_the_stored_refresh_token_through_the_real_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_account_tokens(
        "openai",
        AccountTokens(
            access_token=TOKEN, refresh_token=REFRESH_TOKEN, expires_at=int(time.time()) + 3600
        ),
    )
    oauth, oauth_calls = _oauth_client(
        httpx.Response(
            200,
            json={
                "access_token": FRESH_TOKEN,
                "refresh_token": "rt-rotated",  # pragma: allowlist secret
                "expires_in": 3600,
            },
        )
    )
    real_force_refresh = account_auth.force_refresh
    monkeypatch.setattr(
        account_auth,
        "force_refresh",
        lambda provider, rejected: real_force_refresh(provider, rejected, client=oauth),
    )
    sse, final = _completed_stream()
    upstream = Upstream(httpx.Response(401), _sse_response(sse))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    assert response.json() == final
    assert upstream.calls[1].headers["authorization"] == f"Bearer {FRESH_TOKEN}"
    assert [str(r.url) for r in oauth_calls] == [f"{OPENAI_ISSUER}/oauth/token"]
    form = {k: v[0] for k, v in parse_qs(oauth_calls[0].content.decode()).items()}
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == REFRESH_TOKEN
    stored = load_account_tokens("openai")
    assert stored.access_token == FRESH_TOKEN
    assert stored.refresh_token == "rt-rotated"


def test_401_with_a_revoked_grant_returns_401_and_disables_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_account_tokens("openai", AccountTokens(access_token=TOKEN, refresh_token=REFRESH_TOKEN))
    oauth, _ = _oauth_client(httpx.Response(400, json={"error": "invalid_grant"}))
    real_force_refresh = account_auth.force_refresh
    monkeypatch.setattr(
        account_auth,
        "force_refresh",
        lambda provider, rejected: real_force_refresh(provider, rejected, client=oauth),
    )
    upstream = Upstream(httpx.Response(401, json={"error": "expired"}))
    with upstream.client() as client:
        response = client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    assert response.status_code == 401
    assert len(upstream.calls) == 1
    assert load_account_tokens("openai").disabled_cause


# --------------------------------------------------------------------------- #
# 9. Pass-through for everything that is not a Codex POST
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "url", "content"),
    [
        ("GET", f"{CHATGPT_MODELS_ENDPOINT}?client_version=1", None),
        ("POST", "https://chatgpt.com/backend-api/conversation", b'{"temperature": 1}'),
        ("POST", OPENAI_LLM_ENDPOINT, b'{"temperature": 1, "previous_response_id": "r"}'),
        ("POST", "http://chatgpt.com/backend-api/codex/responses", b'{"temperature": 1}'),
        ("POST", CODEX_URL, b"not json at all"),
        ("POST", CODEX_URL, b"[1, 2, 3]"),
        ("PUT", CODEX_URL, b'{"temperature": 1}'),
    ],
)
def test_non_codex_requests_pass_through_untouched(
    method: str, url: str, content: bytes | None
) -> None:
    upstream = Upstream(httpx.Response(200, json={"ok": True}, headers={"x-up": "1"}))
    with upstream.client() as client:
        response = client.request(
            method, url, content=content, headers={"Authorization": "Bearer caller", "x-a": "b"}
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert AGGREGATED_HEADER not in response.headers
    sent = upstream.calls[0]
    assert sent.content == (content or b"")
    assert sent.headers.get_list("authorization") == ["Bearer caller"]
    assert sent.headers["x-a"] == "b"
    for codex_only in ("originator", "version", "openai-beta", "session_id"):
        assert codex_only not in sent.headers


def test_mounted_transport_routes_codex_posts_and_passes_other_chatgpt_requests() -> None:
    options = provider_client_options(CODEX_URL)
    transport = options["mounts"]["https://chatgpt.com"]
    sse, final = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    transport._inner = httpx.MockTransport(upstream)

    with httpx.Client(**options) as client:
        aggregated = client.post(CODEX_URL, json=_caller_body(), headers=_auth())
        upstream.queue = [httpx.Response(200, json={"models": []})]
        listed = client.get(CHATGPT_MODELS_ENDPOINT, headers=_auth())

    assert aggregated.json() == final
    assert aggregated.headers[AGGREGATED_HEADER] == AGGREGATED_VALUE
    assert listed.json() == {"models": []}
    assert upstream.calls[1].method == "GET"
    assert "originator" not in upstream.calls[1].headers


# --------------------------------------------------------------------------- #
# 10. Async transport
# --------------------------------------------------------------------------- #


def test_async_transport_aggregates_a_non_streaming_call() -> None:
    sse, final = _completed_stream()
    upstream = Upstream(_sse_response(sse))

    async def run() -> httpx.Response:
        async with upstream.async_client() as client:
            return await client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    response = asyncio.run(run())
    assert response.json() == final
    assert response.headers[AGGREGATED_HEADER] == AGGREGATED_VALUE
    assert upstream.body()["store"] is False
    assert upstream.calls[0].headers.get_list("authorization") == [f"Bearer {TOKEN}"]
    assert upstream.calls[0].headers["chatgpt-account-id"] == ACCOUNT_ID


def test_async_transport_with_an_async_handler_passes_streams_through() -> None:
    sse, _ = _completed_stream()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _sse_response(sse)

    async def run() -> httpx.Response:
        transport = AsyncCodexTransport(inner=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post(CODEX_URL, json=_caller_body(stream=True), headers=_auth())

    response = asyncio.run(run())
    assert response.content == sse
    assert AGGREGATED_HEADER not in response.headers
    assert json.loads(calls[0].content)["stream"] is True


def test_async_transport_401_refreshes_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh = RefreshRecorder(FRESH_TOKEN)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    sse, final = _completed_stream()
    upstream = Upstream(httpx.Response(401, json={"error": "expired"}), _sse_response(sse))

    async def run() -> httpx.Response:
        async with upstream.async_client() as client:
            return await client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    response = asyncio.run(run())
    assert response.json() == final
    assert refresh.calls == [("openai", TOKEN)]
    assert upstream.calls[1].headers.get_list("authorization") == [f"Bearer {FRESH_TOKEN}"]


@pytest.mark.parametrize(
    "outcome",
    [AccountAuthError("http", "refresh request failed"), TOKEN],
    ids=["refresh-raises", "same-token"],
)
def test_async_transport_401_without_new_token_returns_401(
    outcome: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh = RefreshRecorder(outcome)
    monkeypatch.setattr(account_auth, "force_refresh", refresh)
    upstream = Upstream(httpx.Response(401, json={"error": "expired"}))

    async def run() -> httpx.Response:
        async with upstream.async_client() as client:
            return await client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    response = asyncio.run(run())
    assert response.status_code == 401
    assert response.json() == {"error": "expired"}
    assert len(upstream.calls) == 1


def test_async_transport_maps_failures_and_passes_errors_through() -> None:
    failed = _response("failed", output=[], error={"code": "server_error", "message": "boom"})
    upstream = Upstream(
        _sse_response(_sse({"type": "response.failed", "response": failed})),
        httpx.Response(429, json={"error": "slow"}),
    )

    async def run() -> tuple[httpx.Response, httpx.Response]:
        async with upstream.async_client() as client:
            first = await client.post(CODEX_URL, json=_caller_body(), headers=_auth())
            second = await client.post(CODEX_URL, json=_caller_body(), headers=_auth())
            return first, second

    first, second = asyncio.run(run())
    error = extract_response_payload_error(first.json())
    assert error is not None and "boom" in str(error)
    assert second.status_code == 429
    assert second.json() == {"error": "slow"}


def test_async_mounted_transport_from_provider_client_options() -> None:
    options = provider_client_options(CODEX_URL, asynchronous=True)
    transport = options["mounts"]["https://chatgpt.com"]
    assert isinstance(transport, AsyncCodexTransport)
    sse, final = _completed_stream()
    upstream = Upstream(_sse_response(sse))
    transport._inner = httpx.MockTransport(upstream)

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(**options) as client:
            return await client.post(CODEX_URL, json=_caller_body(), headers=_auth())

    assert asyncio.run(run()).json() == final


# --------------------------------------------------------------------------- #
# 11. provider_client_options
# --------------------------------------------------------------------------- #


def test_provider_client_options_mounts_codex_transport_for_chatgpt() -> None:
    sync_options = provider_client_options(CODEX_URL)
    async_options = provider_client_options(CODEX_URL, asynchronous=True)

    assert sync_options["follow_redirects"] is False
    assert set(sync_options["mounts"]) == {"https://chatgpt.com"}
    assert type(sync_options["mounts"]["https://chatgpt.com"]) is CodexTransport
    assert type(async_options["mounts"]["https://chatgpt.com"]) is AsyncCodexTransport
    assert "trust_env" not in sync_options


@pytest.mark.parametrize(
    "endpoint",
    [
        OPENAI_LLM_ENDPOINT,
        "https://api.x.ai/v1/responses",
        LIBRAXIS_LLM_ENDPOINT,
        "https://chatgpt.com/backend-api/conversation",
        "https://gateway.example.test/v1/responses",
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_provider_client_options_has_no_codex_mount_elsewhere(
    endpoint: str, asynchronous: bool
) -> None:
    options = provider_client_options(endpoint, asynchronous=asynchronous)
    assert options == {"follow_redirects": False}


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8080/v1/responses", "http://localhost/v1"])
def test_provider_client_options_keeps_loopback_direct(endpoint: str) -> None:
    options = provider_client_options(endpoint)
    assert options == {"follow_redirects": False, "trust_env": False, "mounts": {"http://": None}}


def test_provider_client_options_refuses_remote_plaintext() -> None:
    with pytest.raises(UnsafeEndpointError):
        provider_client_options("http://chatgpt.com/backend-api/codex/responses")


# --------------------------------------------------------------------------- #
# list_models
# --------------------------------------------------------------------------- #


def test_list_models_queries_with_codex_identity_and_keeps_valid_slugs() -> None:
    upstream = Upstream(
        httpx.Response(
            200,
            json={"models": [{"slug": "gpt-5.6-luna"}, {"slug": 7}, "junk", {"slug": "gpt-5.6"}]},
        )
    )
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        models = list_models(TOKEN, client=client)

    assert models == ["gpt-5.6-luna", "gpt-5.6"]
    request = upstream.calls[0]
    assert request.method == "GET"
    assert request.url.params["client_version"] == CODEX_CLIENT_VERSION
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["chatgpt-account-id"] == ACCOUNT_ID
    assert request.headers["accept"] != "text/event-stream"
    assert "content-type" not in request.headers


def test_list_models_tolerates_a_payload_without_models_and_raises_on_http_errors() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    ) as client:
        assert list_models(TOKEN, client=client) == []
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        list_models(TOKEN, client=client)


# --------------------------------------------------------------------------- #
# 12. Config sovereignty: chatgpt.com takes the account token and nothing else
# --------------------------------------------------------------------------- #


def _chatgpt_config(**keys: str) -> ScreenScribeConfig:
    config = ScreenScribeConfig.provider_preset("chatgpt", "")
    for name, value in keys.items():
        setattr(config, name, value)
    return config


def _sign_in(token: str = TOKEN) -> None:
    store_account_tokens(
        "openai", AccountTokens(access_token=token, expires_at=int(time.time()) + 3600)
    )


@pytest.mark.parametrize(
    "keys",
    [
        {"llm_api_key": SK_OPENAI, "vision_api_key": SK_OPENAI},
        {"llm_api_key": LIBRAXIS_KEY, "vision_api_key": LIBRAXIS_KEY},
        {"api_key": SK_GENERIC},
        {"api_key": LIBRAXIS_KEY, "llm_api_key": SK_OPENAI, "vision_api_key": XAI_KEY},
    ],
    ids=["openai-keys", "libraxis-keys", "generic-key", "mixed"],
)
def test_explicit_keys_never_reach_chatgpt_only_the_account_token(keys: dict[str, str]) -> None:
    config = _chatgpt_config(**keys)
    assert config.get_llm_api_key() == ""
    assert config.get_vision_api_key() == ""

    _sign_in()
    assert config.get_llm_api_key() == TOKEN
    assert config.get_vision_api_key() == TOKEN


def test_unsigned_chatgpt_config_asks_for_the_openai_sign_in() -> None:
    config = _chatgpt_config(llm_api_key=SK_OPENAI, stt_api_key=LIBRAXIS_KEY)
    assert config.uses_chatgpt_account()
    assert config.validate() == []
    status = config.configuration_status()
    assert status == f"INCOMPLETE - {CHATGPT_SIGN_IN_HINT}"
    assert "screenscribe auth login openai" in status


def test_signed_in_chatgpt_config_is_ready_with_an_stt_key() -> None:
    _sign_in()
    config = _chatgpt_config(stt_api_key=LIBRAXIS_KEY)
    assert config.configuration_status() == "READY"
    assert config.get_stt_api_key() == LIBRAXIS_KEY


def test_disabled_account_yields_no_bearer_for_chatgpt() -> None:
    store_account_tokens(
        "openai", AccountTokens(access_token=TOKEN, disabled_cause="refresh refused")
    )
    config = _chatgpt_config(llm_api_key=SK_OPENAI)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert config.get_llm_api_key() == ""


def test_api_openai_com_never_receives_the_account_token_and_warns_once() -> None:
    _sign_in()
    config = ScreenScribeConfig.provider_preset("openai", "")

    with pytest.warns(UserWarning, match="identity-only"):
        assert config.get_llm_api_key() == ""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert config.get_vision_api_key() == ""
        assert config.get_llm_api_key() == ""
    assert not [w for w in caught if "identity-only" in str(w.message)]

    config.llm_api_key = SK_OPENAI
    assert config.get_llm_api_key() == SK_OPENAI


def test_account_only_hosts_cover_chatgpt() -> None:
    assert "chatgpt.com" in ScreenScribeConfig._ACCOUNT_ONLY_HOSTS
    assert ScreenScribeConfig._ACCOUNT_HOSTS["chatgpt.com"] == "openai"
    _sign_in()
    assert ScreenScribeConfig._key_or_account_bearer(SK_OPENAI, CODEX_URL) == TOKEN
    assert ScreenScribeConfig._key_or_account_bearer(SK_OPENAI, OPENAI_LLM_ENDPOINT) == SK_OPENAI


# --------------------------------------------------------------------------- #
# 13. provider_preset("chatgpt", stt_from=...) and validate()
# --------------------------------------------------------------------------- #


def test_chatgpt_preset_carries_stt_and_ignores_api_key() -> None:
    source = ScreenScribeConfig(
        provider="libraxis",
        api_key=LIBRAXIS_KEY,
        stt_model="whisper-large-fixture",
        language="pl",
        stt_fallback_endpoint=OPENAI_STT_ENDPOINT,
        stt_fallback_api_key=SK_OPENAI,
        stt_fallback_model="whisper-1",
    )
    source._libraxis_key_values = {LIBRAXIS_KEY}

    preset = ScreenScribeConfig.provider_preset(
        "chatgpt", SK_GENERIC, llm_model="gpt-5.6", stt_from=source
    )

    assert preset.provider == "chatgpt"
    assert preset.api_key == ""
    assert preset.llm_api_key == preset.vision_api_key == ""
    assert preset.llm_endpoint == preset.vision_endpoint == CODEX_URL
    assert preset.llm_model == "gpt-5.6"
    assert preset.stt_endpoint == LIBRAXIS_STT_ENDPOINT
    assert preset.stt_model == "whisper-large-fixture"
    assert preset.stt_api_key == LIBRAXIS_KEY
    assert preset.language == "pl"
    assert preset.stt_fallback_endpoint == OPENAI_STT_ENDPOINT
    assert preset.stt_fallback_api_key == SK_OPENAI
    assert preset._libraxis_key_values == {LIBRAXIS_KEY}
    assert preset.get_stt_api_key() == LIBRAXIS_KEY
    assert preset.validate() == []


def test_chatgpt_preset_saves_a_carried_libraxis_key_as_libraxis_api_key(
    tmp_path: Path,
) -> None:
    source = ScreenScribeConfig(provider="libraxis", api_key=LIBRAXIS_KEY)
    source._libraxis_key_values = {LIBRAXIS_KEY}
    preset = ScreenScribeConfig.provider_preset("chatgpt", "", stt_from=source)

    path = preset.save_default_config()

    assert path == tmp_path / ".config" / "screenscribe" / "config.env"
    lines = path.read_text().splitlines()
    assert f"LIBRAXIS_API_KEY={LIBRAXIS_KEY}" in lines
    assert "SCREENSCRIBE_API_KEY=" in lines
    assert not any(line.startswith("SCREENSCRIBE_STT_API_KEY=") for line in lines)
    assert "SCREENSCRIBE_PROVIDER=chatgpt" in lines
    assert f"SCREENSCRIBE_LLM_ENDPOINT={CODEX_URL}" in lines
    assert f"SCREENSCRIBE_VISION_ENDPOINT={CODEX_URL}" in lines
    assert f"SCREENSCRIBE_STT_ENDPOINT={LIBRAXIS_STT_ENDPOINT}" in lines


def test_chatgpt_preset_without_stt_from_uses_builtin_libraxis_stt() -> None:
    preset = ScreenScribeConfig.provider_preset("chatgpt", SK_OPENAI)
    assert preset.stt_endpoint == LIBRAXIS_STT_ENDPOINT
    assert preset.api_key == preset.stt_api_key == ""
    assert preset._libraxis_key_values == set()


@pytest.mark.parametrize("stt_provider", ["libraxis", "openai", "xai"])
def test_validate_is_clean_for_chatgpt_with_any_stt(stt_provider: str) -> None:
    key = {"libraxis": LIBRAXIS_KEY, "openai": SK_OPENAI, "xai": XAI_KEY}[stt_provider]
    source = ScreenScribeConfig.provider_preset(stt_provider, key)
    if stt_provider == "libraxis":
        source._libraxis_key_values = {key}
    preset = ScreenScribeConfig.provider_preset("chatgpt", "", stt_from=source)

    assert preset.stt_endpoint == source.stt_endpoint
    assert preset.stt_api_key == key
    assert preset.validate() == []
    _sign_in()
    assert preset.validate() == []
    assert preset.configuration_status() == "READY"
    if stt_provider == "xai":
        assert preset.stt_endpoint == XAI_STT_ENDPOINT
        assert preset.tts_endpoint == source.tts_endpoint
        assert preset.stt_live_endpoint == source.stt_live_endpoint


def test_libraxis_key_carried_into_chatgpt_is_fenced_from_a_foreign_stt_host() -> None:
    source = ScreenScribeConfig(provider="libraxis", api_key=LIBRAXIS_KEY)
    source._libraxis_key_values = {LIBRAXIS_KEY}
    preset = ScreenScribeConfig.provider_preset("chatgpt", "", stt_from=source)
    preset.stt_endpoint = OPENAI_STT_ENDPOINT
    assert preset.get_stt_api_key() == ""


@pytest.mark.parametrize(
    ("field_name", "endpoint", "label"),
    [
        ("llm_endpoint", OPENAI_LLM_ENDPOINT, "LLM"),
        ("llm_endpoint", LIBRAXIS_LLM_ENDPOINT, "LLM"),
        ("vision_endpoint", "https://api.x.ai/v1/responses", "Vision"),
    ],
)
def test_validate_rejects_a_chatgpt_preset_pointing_elsewhere(
    field_name: str, endpoint: str, label: str
) -> None:
    preset = ScreenScribeConfig.provider_preset("chatgpt", "")
    setattr(preset, field_name, endpoint)

    errors = preset.validate()
    assert any(
        error.startswith(f"{label} endpoint does not match the configured chatgpt preset")
        for error in errors
    ), errors
    assert preset.configuration_status() == "BLOCKED - provider preset mismatch"


# --------------------------------------------------------------------------- #
# 14. Response chaining guards
# --------------------------------------------------------------------------- #


def test_response_ids_never_chain_onto_the_codex_backend() -> None:
    assert response_id_can_chain(CODEX_URL, CODEX_URL) is False
    assert response_id_can_chain(LIBRAXIS_LLM_ENDPOINT, CODEX_URL) is False
    assert response_id_can_chain(LIBRAXIS_LLM_ENDPOINT, LIBRAXIS_LLM_ENDPOINT) is True


@pytest.mark.parametrize(
    ("llm", "vision"),
    [
        (CODEX_URL, CODEX_URL),
        (CODEX_URL, LIBRAXIS_LLM_ENDPOINT),
        (LIBRAXIS_LLM_ENDPOINT, CODEX_URL),
    ],
)
@pytest.mark.parametrize("text_only", [False, True])
def test_unified_analysis_never_chains_when_either_endpoint_is_chatgpt(
    llm: str, vision: str, text_only: bool
) -> None:
    config = ScreenScribeConfig(llm_endpoint=llm, vision_endpoint=vision)
    assert _may_chain_previous_response(config, use_text_only_backend=text_only) is False


def test_unified_analysis_still_chains_on_a_stateful_provider() -> None:
    config = ScreenScribeConfig()
    assert _may_chain_previous_response(config, use_text_only_backend=True) is True


def _agent_provider(host: str, protocol: str = "responses") -> AgentProvider:
    return AgentProvider(
        name="fixture",
        protocol=protocol,
        key="k",
        model="m",
        url=f"https://{host}/v1/responses",
        trust="processor",
        slot="llm",
        host=host,
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("chatgpt.com", False),
        ("api.x.ai", False),
        ("api.openai.com", True),
        ("api.libraxis.cloud", True),
    ],
)
def test_agent_stateful_chain_is_off_for_chatgpt(host: str, expected: bool) -> None:
    assert _supports_stateful_response_chain(_agent_provider(host)) is expected
