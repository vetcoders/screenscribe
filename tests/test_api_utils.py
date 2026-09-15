"""Unit tests for api_utils.extract_llm_response_text type-guard branches."""

import httpx
import pytest

from screenscribe.api_utils import (
    extract_llm_response_text,
    retry_after_seconds,
    retry_request,
)


def _http_429(retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    request = httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")
    response = httpx.Response(429, headers=headers, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def _http_status(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("status error", request=request, response=response)


@pytest.mark.parametrize("status_code", [401, 403])
def test_retry_request_does_not_retry_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    slept: list[float] = []
    calls = 0
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda d: slept.append(d))

    def auth_error() -> None:
        nonlocal calls
        calls += 1
        raise _http_status(status_code)

    with pytest.raises(httpx.HTTPStatusError):
        retry_request(auth_error, max_retries=3, operation_name="STT")

    assert calls == 1
    assert slept == []


def test_retry_after_seconds_parses_delta_seconds() -> None:
    assert retry_after_seconds(_http_429("7")) == 7.0


def test_retry_after_seconds_none_without_header() -> None:
    assert retry_after_seconds(_http_429(None)) is None


def test_retry_after_seconds_none_for_http_date_form() -> None:
    # HTTP-date form is intentionally not parsed; caller falls back to backoff.
    assert retry_after_seconds(_http_429("Wed, 21 Oct 2026 07:28:00 GMT")) is None


def test_retry_request_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 carrying Retry-After must drive the sleep, not exponential backoff."""
    slept: list[float] = []
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda d: slept.append(d))

    def always_429() -> None:
        raise _http_429("9")

    with pytest.raises(httpx.HTTPStatusError):
        retry_request(always_429, max_retries=1, base_delay=1.0, operation_name="STT")

    # Exactly one retry sleep, equal to the server-advertised Retry-After.
    assert slept == [9.0]


def test_retry_request_caps_absurd_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda d: slept.append(d))

    def always_429() -> None:
        raise _http_429("99999")

    with pytest.raises(httpx.HTTPStatusError):
        retry_request(always_429, max_retries=1, operation_name="STT")

    assert slept == [120.0]  # RETRY_AFTER_MAX_SECONDS ceiling


def test_retry_request_falls_back_to_backoff_without_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda d: slept.append(d))

    def always_429() -> None:
        raise _http_429(None)

    with pytest.raises(httpx.HTTPStatusError):
        retry_request(always_429, max_retries=1, base_delay=1.0, operation_name="STT")

    # One retry, delay from jittered exponential backoff (not a fixed header value).
    assert len(slept) == 1
    assert 0.5 <= slept[0] < 1.5  # base_delay(1.0) * 2**0 * jitter(0.5..1.5)


class TestExtractLlmResponseTextResponsesAPI:
    """Tests for the default LibraxisAI (OpenAI-compatible) Responses API format."""

    ENDPOINT = "https://api.libraxis.cloud/v1/responses"

    def test_direct_output_text_string(self) -> None:
        response = {"output_text": "Direct text response."}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Direct text response."

    def test_direct_output_text_dict(self) -> None:
        response = {"output_text": {"text": "Nested text."}}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Nested text."

    def test_direct_output_text_list(self) -> None:
        response = {"output_text": [{"text": "Part A"}, {"text": " Part B"}]}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Part A Part B"

    def test_direct_output_text_list_skips_non_dict(self) -> None:
        response = {"output_text": [42, {"text": "Valid"}]}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Valid"

    def test_text_field_fallback(self) -> None:
        response = {"text": "Fallback text."}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Fallback text."

    def test_content_field_fallback(self) -> None:
        response = {"content": "Content fallback."}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Content fallback."

    def test_output_array_message_type(self) -> None:
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "Message text."},
                    ],
                }
            ]
        }
        assert extract_llm_response_text(response, self.ENDPOINT) == "Message text."

    def test_output_array_output_text_type(self) -> None:
        response = {
            "output": [
                {"type": "output_text", "text": "Direct output item."},
            ]
        }
        assert extract_llm_response_text(response, self.ENDPOINT) == "Direct output item."

    def test_output_array_skips_reasoning(self) -> None:
        response = {
            "output": [
                {"type": "reasoning", "text": "Thinking..."},
                {"type": "output_text", "text": "Answer."},
            ]
        }
        assert extract_llm_response_text(response, self.ENDPOINT) == "Answer."

    def test_output_not_list_returns_empty(self) -> None:
        response = {"output": "not a list"}
        assert extract_llm_response_text(response, self.ENDPOINT) == ""

    def test_empty_response(self) -> None:
        assert extract_llm_response_text({}, self.ENDPOINT) == ""

    def test_output_text_non_string_value(self) -> None:
        response = {
            "output": [
                {"type": "output_text", "text": 123},
            ]
        }
        assert extract_llm_response_text(response, self.ENDPOINT) == ""

    def test_message_content_non_list(self) -> None:
        response = {
            "output": [
                {"type": "message", "content": "not a list"},
            ]
        }
        assert extract_llm_response_text(response, self.ENDPOINT) == ""


class TestExtractLlmResponseTextChatCompletions:
    """Tests for OpenAI Chat Completions format."""

    ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def test_standard_choices(self) -> None:
        response = {"choices": [{"message": {"content": "Hello from ChatGPT."}}]}
        assert extract_llm_response_text(response, self.ENDPOINT) == "Hello from ChatGPT."

    def test_empty_choices(self) -> None:
        response: dict[str, list[object]] = {"choices": []}
        assert extract_llm_response_text(response, self.ENDPOINT) == ""

    def test_no_choices(self) -> None:
        assert extract_llm_response_text({}, self.ENDPOINT) == ""

    def test_non_string_content(self) -> None:
        response = {"choices": [{"message": {"content": ["not", "string"]}}]}
        assert extract_llm_response_text(response, self.ENDPOINT) == ""


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "Infinity", "-Infinity", "NaN"])
def test_retry_after_seconds_none_for_non_finite(raw: str) -> None:
    """C6.4/A5: a non-finite Retry-After header (which float() parses but is
    +Inf/NaN) must return None so the caller falls back to exponential backoff,
    instead of stalling forever (+Inf) or relying on max(0.0, nan)==0.0."""
    assert retry_after_seconds(_http_429(raw)) is None


def test_retry_after_seconds_still_parses_finite() -> None:
    """C6.4/A5 regression guard: finite values are unaffected by the guard."""
    assert retry_after_seconds(_http_429("7")) == 7.0


# --- stream error events -----------------------------------------------------


def test_extract_stream_error_event_shapes() -> None:
    from screenscribe.api_utils import extract_stream_error_event

    assert extract_stream_error_event({"type": "response.output_text.delta", "delta": "x"}) is None
    assert extract_stream_error_event({"type": "response.completed", "response": {}}) is None

    nested = extract_stream_error_event(
        {"type": "error", "error": {"type": "server_error", "message": "boom"}}
    )
    assert nested is not None and nested.transient and "boom" in str(nested)

    failed = extract_stream_error_event(
        {"type": "response.failed", "response": {"error": {"code": "invalid_prompt"}}}
    )
    assert failed is not None and not failed.transient
    assert "invalid_prompt" in str(failed)

    completed_failed = extract_stream_error_event(
        {"type": "response.completed", "response": {"status": "failed"}}
    )
    assert str(completed_failed) == "Streaming response completed with failed status."

    incomplete = extract_stream_error_event(
        {
            "type": "response.incomplete",
            "response": {"incomplete_details": {"reason": "content_filter"}},
        }
    )
    assert incomplete is not None
    assert incomplete.event_type == "response.incomplete"
    assert "content_filter" in str(incomplete)

    assert extract_stream_error_event({"type": "error", "error": "not a dict"}) is not None


def test_retry_request_retries_only_transient_stream_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screenscribe.api_utils import StreamEventError

    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda _d: None)
    calls = 0

    def transient_then_ok() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StreamEventError("overloaded", code="overloaded_error", transient=True)
        return "ok"

    assert retry_request(transient_then_ok) == "ok"
    assert calls == 2

    permanent_calls = 0

    def permanent() -> str:
        nonlocal permanent_calls
        permanent_calls += 1
        raise StreamEventError("bad prompt", code="invalid_prompt")

    with pytest.raises(StreamEventError):
        retry_request(permanent)
    assert permanent_calls == 1


def test_endpoint_host_hides_credentials() -> None:
    from screenscribe.api_utils import endpoint_host

    url = "https://user:secret@llm.example.com:8443/v1/responses?k=1"  # pragma: allowlist secret
    assert endpoint_host(url) == "llm.example.com"
    assert endpoint_host("") == "unknown host"


# --- URL redaction for printed / logged error messages -----------------------

_USERINFO_URL = "https://user:secret@api.example.com/v1/responses?key=abc&api-version=1"  # pragma: allowlist secret


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:secret@api.example.com/v1/responses",  # pragma: allowlist secret
            "https://***@api.example.com/v1/responses",
        ),
        (
            "https://api.example.com/v1/responses?key=abc&api-version=2024-01-01",
            "https://api.example.com/v1/responses?key=***&api-version=***",
        ),
        ("https://api.example.com/v1/responses", "https://api.example.com/v1/responses"),
        ("http://localhost:8443/v1/responses", "http://localhost:8443/v1/responses"),
        ("https://api.example.com/v1/responses#frag", "https://api.example.com/v1/responses"),
        ("https://api.example.com/v1?sk-token-only", "https://api.example.com/v1?***"),
        ("not a url at all", "unknown host"),
        ("https://api.example.com:99999/bad-port", "unknown host"),
        ("", "unknown host"),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    from screenscribe.api_utils import redact_url

    assert redact_url(url) == expected


def _assert_url_secrets_absent(text: str) -> None:
    assert "secret" not in text
    assert "user:" not in text
    assert "abc" not in text


def test_redact_error_message_http_status_error() -> None:
    from screenscribe.api_utils import redact_error_message

    request = httpx.Request("POST", _USERINFO_URL)
    response = httpx.Response(401, request=request)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    redacted = redact_error_message(caught.value)

    _assert_url_secrets_absent(redacted)
    assert redacted.splitlines()[0] == (
        "Client error '401 Unauthorized' for url "
        "'https://***@api.example.com/v1/responses?key=***&api-version=***'"
    )


def test_redact_error_message_plain_error_with_embedded_url() -> None:
    from screenscribe.api_utils import redact_error_message

    redacted = redact_error_message(RuntimeError(f"call to ({_USERINFO_URL}), failed"))

    assert redacted == (
        "call to (https://***@api.example.com/v1/responses?key=***&api-version=***), failed"
    )


def test_redact_error_message_request_error_without_request() -> None:
    from screenscribe.api_utils import redact_error_message

    assert redact_error_message(httpx.ConnectError("connection refused")) == "connection refused"
    redacted = redact_error_message(httpx.ReadError(f"read failed: {_USERINFO_URL}"))
    _assert_url_secrets_absent(redacted)


def test_retry_request_log_redacts_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from rich.console import Console

    buffer = io.StringIO()
    monkeypatch.setattr(
        "screenscribe.api_utils.console", Console(file=buffer, width=500, color_system=None)
    )
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda _d: None)
    request = httpx.Request("POST", _USERINFO_URL)
    calls = 0

    def unavailable_then_ok() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            httpx.Response(503, request=request).raise_for_status()
        return "ok"

    assert retry_request(unavailable_then_ok, operation_name="LLM call") == "ok"
    output = buffer.getvalue()
    _assert_url_secrets_absent(output)
    error_line = next(line for line in output.splitlines() if line.startswith("  Error:"))
    assert error_line == (
        "  Error: Server error '503 Service Unavailable' for url "
        "'https://***@api.example.com/v1/responses?key=***&api-version=***'"
    )


# --- Non-streaming Responses bodies: answer extraction and status errors -----


def test_extract_llm_response_text_skips_reasoning_item() -> None:
    from screenscribe.api_utils import extract_llm_response_text

    payload = {
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
            {"type": "message", "content": [{"type": "output_text", "text": "ANSWER"}]},
        ],
        "text": {"format": {"type": "text"}},
        "reasoning": {"effort": "medium", "summary": "auto"},
    }

    assert extract_llm_response_text(payload, "https://api.example.com/v1/responses") == "ANSWER"


def test_build_llm_request_body_reasoning_effort() -> None:
    from screenscribe.api_utils import build_llm_request_body

    responses = build_llm_request_body(
        "m", "p", "https://api.example.com/v1/responses", reasoning_effort="low"
    )
    chat = build_llm_request_body(
        "m", "p", "https://api.example.com/v1/chat/completions", reasoning_effort="low"
    )
    plain = build_llm_request_body("m", "p", "https://api.example.com/v1/responses")

    assert responses["reasoning"] == {"summary": "auto", "effort": "low"}
    assert "reasoning" not in chat
    assert "reasoning" not in plain


def test_extract_response_payload_error() -> None:
    from screenscribe.api_utils import extract_response_payload_error

    assert extract_response_payload_error({"status": "completed", "error": None}) is None
    failed = extract_response_payload_error(
        {"status": "failed", "error": {"code": "server_error", "message": "rejected"}}
    )
    incomplete = extract_response_payload_error(
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    )
    error_only = extract_response_payload_error({"error": {"message": "bad request"}})

    assert str(failed) == "rejected (code: server_error)"
    assert str(incomplete) == "Response incomplete: max_output_tokens"
    assert str(error_only) == "bad request"


# --- Provider-supplied text is URL-redacted at the source ---------------------

_GATEWAY_URL = "https://user:secret@gw.example.com/x?key=abc"  # pragma: allowlist secret


def _assert_gateway_secrets_absent(text: str) -> None:
    assert "secret" not in text
    assert "key=abc" not in text
    assert "user:" not in text


def test_stream_event_error_redacts_provider_message_and_code() -> None:
    from screenscribe.api_utils import StreamEventError

    error = StreamEventError(f"upstream {_GATEWAY_URL} refused", code=f"bad_url:{_GATEWAY_URL}")

    _assert_gateway_secrets_absent(str(error))
    _assert_gateway_secrets_absent(error.code)
    assert str(error) == (
        "upstream https://***@gw.example.com/x?key=*** refused "
        "(code: bad_url:https://***@gw.example.com/x?key=***)"
    )


def test_extract_response_payload_error_redacts_urls() -> None:
    from screenscribe.api_utils import extract_response_payload_error

    error = extract_response_payload_error(
        {"status": "failed", "error": {"code": "server_error", "message": f"via {_GATEWAY_URL}"}}
    )

    assert str(error) == "via https://***@gw.example.com/x?key=*** (code: server_error)"


def test_redaction_handles_uppercase_scheme() -> None:
    from screenscribe.api_utils import redact_error_message, redact_urls_in_text

    upper = "HTTPS://user:secret@GW.example.com/x?key=abc"  # pragma: allowlist secret

    in_text = redact_urls_in_text(f"call {upper} failed")
    in_error = redact_error_message(RuntimeError(f"call {upper} failed"))

    for redacted in (in_text, in_error):
        assert "secret" not in redacted
        assert "key=abc" not in redacted
        assert "user:" not in redacted
    assert in_text == "call https://***@gw.example.com/x?key=*** failed"
