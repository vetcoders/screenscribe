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
