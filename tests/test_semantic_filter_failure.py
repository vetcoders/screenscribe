"""Failure-mode tests for the semantic pre-filter.

The pre-filter is the heart of the product: every review depends on it. A
network / auth / stream failure must be reported as a FAILURE (``failed=True``)
so the pipeline can stop honestly and offer ``--resume`` -- never silently
collapsed into an empty "no issues" result that is indistinguishable from a
clean run on a healthy key.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

import httpx
import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.semantic_filter import semantic_prefilter
from screenscribe.transcribe import Segment, TranscriptionResult


@pytest.fixture
def transcription() -> TranscriptionResult:
    """A short transcript with one clearly actionable segment."""
    return TranscriptionResult(
        text="The save button does nothing when I click it.",
        segments=[
            Segment(
                id=0,
                start=0.0,
                end=4.0,
                text="The save button does nothing when I click it.",
            ),
        ],
        language="en",
    )


@pytest.fixture
def config() -> ScreenScribeConfig:
    """Config with an LLM key set (so the no-key guard does not short-circuit)."""
    return ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        language="en",
    )


def _client_raising_at_stream(exc: Exception) -> type:
    """An httpx.Client stand-in whose streaming POST raises ``exc``."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            raise exc

    return _Client


def _client_raising_at_raise_for_status(exc: Exception) -> type:
    """An httpx.Client stand-in whose streamed response fails ``raise_for_status``."""

    class _Stream:
        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            raise exc

        def iter_lines(self) -> Iterator[str]:
            return iter([])

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _Stream:
            return _Stream()

    return _Client


def _client_empty_ok() -> type:
    """An httpx.Client stand-in that streams a valid, empty POI list (200 OK)."""

    class _Stream:
        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[str]:
            yield (
                'data: {"type": "response.output_text.delta", '
                '"delta": "{\\"points_of_interest\\": []}"}'
            )
            yield "data: [DONE]"

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _Stream:
            return _Stream()

    return _Client


def _client_garbage_ok() -> type:
    """An httpx.Client stand-in that streams a 200 OK with non-JSON content."""

    class _Stream:
        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[str]:
            yield (
                'data: {"type": "response.output_text.delta", '
                '"delta": "I cannot analyze this transcript."}'
            )
            yield "data: [DONE]"

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _Stream:
            return _Stream()

    return _Client


def test_prefilter_marks_failed_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A network drop must surface as failed=True, not a silent empty result."""
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_raising_at_stream(httpx.ConnectError("connection refused")),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.pois == []


def test_prefilter_marks_failed_on_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A 401/429 (raise_for_status) must surface as failed=True, not empty success."""
    request = httpx.Request("POST", config.llm_endpoint)
    response = httpx.Response(401, request=request)
    err = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_raising_at_raise_for_status(err),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.pois == []


def test_prefilter_marks_failed_when_no_llm_key(
    transcription: TranscriptionResult,
) -> None:
    """Reaching the prefilter without an LLM key is a failure, not zero findings."""
    config = ScreenScribeConfig(
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        language="en",
    )
    assert not config.get_llm_api_key()

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.pois == []


def test_prefilter_empty_response_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A healthy LLM that genuinely finds nothing is failed=False (real empty)."""
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_empty_ok(),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert result.pois == []


def test_prefilter_marks_failed_on_unparseable_content(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A 200 OK whose content is not valid JSON is a failure, not zero findings.

    The model 'responded' but produced garbage we cannot parse -- that must not
    masquerade as a clean 'no issues' result.
    """
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_garbage_ok(),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.pois == []


def _client_no_content() -> type:
    """An httpx.Client stand-in that streams a 200 OK but yields NO content."""

    class _Stream:
        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[str]:
            # Stream opens cleanly but the model emits no output text at all.
            yield "data: [DONE]"

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _Stream:
            return _Stream()

    return _Client


def test_prefilter_marks_failed_on_empty_content(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A 200 OK that streams NO content is a failure, not a clean zero-POI result.

    An empty body is not the same as a healthy ``{"points_of_interest": []}``:
    the model returned nothing usable, so the stage must fail loudly rather than
    masquerade as 'no issues detected'.
    """
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_no_content(),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.pois == []


# --- A1: retry transient pre-filter failures (honor Retry-After) -------------
#
# A transient 429/5xx/network drop in the pre-filter must be RETRIED (honoring
# Retry-After) before it becomes failed=True. Without retry one fluke 429 at
# 10-concurrent collapses straight into an honest-but-avoidable failure. The
# retry pattern (api_utils.retry_request) already powers STT and the unified VLM.

# A single SSE delta carrying one well-formed point of interest, then [DONE].
_OK_POI_DELTA = (
    'data: {"type": "response.output_text.delta", '
    '"delta": "{'
    '\\"points_of_interest\\": [{'
    '\\"timestamp_start\\": 0.0, '
    '\\"timestamp_end\\": 4.0, '
    '\\"category\\": \\"bug\\", '
    '\\"confidence\\": 0.9, '
    '\\"reasoning\\": \\"save button does nothing\\", '
    '\\"transcript_excerpt\\": \\"save\\"'
    '}]}"}'
)
_OK_LINES = [_OK_POI_DELTA, "data: [DONE]"]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make retry backoff instant and record the delays it would have slept.

    Retry now runs through ``api_utils.retry_request``, which sleeps between
    attempts. Patch the sleep to a no-op so retriable-error tests stay fast, and
    capture each delay so the Retry-After test can assert the honored value.
    """
    delays: list[float] = []

    def _record(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("screenscribe.api_utils.time.sleep", _record)
    return delays


def _sequenced_client(outcomes: list[Any], attempts: list[int]) -> type:
    """An httpx.Client stand-in driving a sequence of streaming attempts.

    ``outcomes[i]`` controls the i-th attempt (later attempts reuse the last
    entry): an ``Exception`` is raised from ``raise_for_status`` (HTTP/transport
    failure); a ``list[str]`` is streamed verbatim by ``iter_lines`` after a
    clean ``raise_for_status``. Each attempt appends its index to ``attempts``.
    """

    class _Stream:
        def __init__(self, outcome: Any) -> None:
            self._outcome = outcome

        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            if isinstance(self._outcome, Exception):
                raise self._outcome

        def iter_lines(self) -> Iterator[str]:
            if isinstance(self._outcome, Exception):
                return iter([])
            return iter(self._outcome)

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _Stream:
            idx = len(attempts)
            attempts.append(idx)
            return _Stream(outcomes[min(idx, len(outcomes) - 1)])

    return _Client


def _client_midstream_timeout_then_ok(attempts: list[int]) -> type:
    """First attempt streams a partial delta then raises mid-iteration; then OK.

    Exercises the 'failure inside iteration → retry the whole attempt' path and
    proves a retried attempt starts from clean accumulators (no duplicated POI).
    """

    class _StreamFail:
        def __enter__(self) -> _StreamFail:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[str]:
            yield _OK_POI_DELTA  # partial content streamed before the drop
            raise httpx.ReadTimeout("read timed out mid-stream")

    class _StreamOK:
        def __enter__(self) -> _StreamOK:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[str]:
            yield from _OK_LINES

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            idx = len(attempts)
            attempts.append(idx)
            return _StreamFail() if idx == 0 else _StreamOK()

    return _Client


def _http_status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError for the given status (+ optional headers)."""
    request = httpx.Request("POST", "https://api.example.com/v1/responses")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"status {status}", request=request, response=response)


def test_prefilter_retry_transient_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """One transient 429 then a 200 success → retry recovers, failed=False."""
    attempts: list[int] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _sequenced_client([_http_status_error(429), _OK_LINES], attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert len(result.pois) == 1
    assert len(attempts) == 2  # one failure, recovered on the retry


def test_prefilter_retry_honors_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
    _no_sleep: list[float],
) -> None:
    """A 429 carrying ``Retry-After: 5`` makes the retry wait exactly 5s."""
    attempts: list[int] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _sequenced_client([_http_status_error(429, {"Retry-After": "5"}), _OK_LINES], attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert _no_sleep == [pytest.approx(5.0)]


def test_prefilter_retry_exhausted_429_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """Persistent 429 across all attempts → failed=True after retries exhaust."""
    attempts: list[int] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _sequenced_client([_http_status_error(429)], attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.pois == []
    assert len(attempts) == 4  # initial attempt + 3 retries (max_retries=3)


def test_prefilter_retry_skips_auth_401(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A 401 is not retriable → single attempt, immediate failed=True."""
    attempts: list[int] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _sequenced_client([_http_status_error(401)], attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert len(attempts) == 1  # auth failure: no retry storm


def test_prefilter_retry_midstream_timeout_no_duplicate_pois(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A mid-stream timeout retries the whole attempt without duplicating POIs."""
    attempts: list[int] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_midstream_timeout_then_ok(attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert len(result.pois) == 1  # only the clean retry's content, not 1 + partial
    assert len(attempts) == 2


# --- Provider error events inside a 200 stream ------------------------------
#
# A Responses-API stream can fail AFTER the 200 status by sending `error`,
# `response.failed` or `response.incomplete` events. The user must see the
# provider's concrete reason, not a generic "Empty response".


def _event(payload: dict[str, Any]) -> str:
    import json

    return "data: " + json.dumps(payload)


def test_prefilter_response_failed_event_carries_provider_message(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A non-transient ``response.failed`` surfaces its message, with no retry."""
    attempts: list[int] = []
    lines = [
        _event({"type": "response.created", "response": {"id": "resp_1"}}),
        _event(
            {
                "type": "response.failed",
                "response": {
                    "id": "resp_1",
                    "status": "failed",
                    "error": {"code": "invalid_prompt", "message": "Prompt was rejected"},
                },
            }
        ),
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client", _sequenced_client([lines], attempts)
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "Prompt was rejected" in result.error
    assert "invalid_prompt" in result.error
    assert "Empty response" not in result.error
    assert len(attempts) == 1


def test_prefilter_incomplete_event_names_the_reason(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """``response.incomplete`` with no text reports ``incomplete_details.reason``."""
    lines = [
        _event(
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            }
        ),
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _sequenced_client([lines], []))

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "max_output_tokens" in result.error


def test_prefilter_top_level_error_event_message(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """``{"type": "error", "code", "message"}`` (top-level fields) is captured."""
    lines = [_event({"type": "error", "code": "invalid_api_key", "message": "Key revoked"})]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _sequenced_client([lines], []))

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "Key revoked" in result.error
    assert "invalid_api_key" in result.error


def test_prefilter_transient_error_event_is_retried(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """An in-stream ``server_error`` is transient: retried, then succeeds."""
    attempts: list[int] = []
    failing = [
        _event(
            {"type": "error", "error": {"code": "server_error", "message": "Upstream overloaded"}}
        )
    ]
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _sequenced_client([failing, _OK_LINES], attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert len(result.pois) == 1
    assert len(attempts) == 2


def test_prefilter_transient_error_event_exhausted_keeps_reason(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A persistent transient event fails after retries WITH the provider reason."""
    attempts: list[int] = []
    failing = [
        _event(
            {
                "type": "response.failed",
                "response": {"error": {"code": "rate_limit_exceeded", "message": "Slow down"}},
            }
        )
    ]
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client", _sequenced_client([failing], attempts)
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "Slow down" in result.error
    assert len(attempts) == 4


def test_prefilter_empty_stream_has_clear_reason(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A 200 with zero events reports an empty stream, not a generic empty response."""
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _sequenced_client([[]], []))

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert result.error == "LLM endpoint returned an empty stream (no events)"


def test_prefilter_non_sse_json_error_body_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A 200 plain JSON error body (not an event stream) names the provider error."""
    body = ['{"error": {"message": "Model is loading", "code": "model_not_ready"}}']
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _sequenced_client([body], []))

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "Model is loading" in result.error


def test_prefilter_content_still_parsed_alongside_events(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """Normal content keeps working with the error-event handling in place."""
    lines = [
        _event({"type": "response.created", "response": {"id": "resp_ok"}}),
        _OK_POI_DELTA,
        _event(
            {"type": "response.completed", "response": {"id": "resp_ok", "status": "completed"}}
        ),
        "data: [DONE]",
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _sequenced_client([lines], []))

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert len(result.pois) == 1
    assert result.response_id == "resp_ok"


def test_prefilter_connect_timeout_names_unreachable_host(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A connect/TLS handshake timeout says the LLM host was unreachable."""
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _client_raising_at_stream(httpx.ConnectTimeout("")),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "api.example.com" in result.error
    assert "unreachable" in result.error
    assert "test-key" not in result.error


# --- Reasoning effort + no retry after the model produced output -------------
#
# Live root cause (api.libraxis.cloud, model `programmer`): without an explicit
# reasoning effort the model reasoned in a loop for 11-20 minutes, emitted no
# output_text and ended with response.failed / server_error. Retrying that
# "transient" code would multiply the hang, so a provider error after any model
# output fails fast with an actionable reason.


def _recording_client(lines: list[str], bodies: list[dict[str, Any]], attempts: list[int]) -> type:
    """Like ``_sequenced_client`` but records each request JSON body."""
    base = _sequenced_client([lines], attempts)

    class _Client(base):  # type: ignore[misc,valid-type]
        def stream(self, *args: Any, **kwargs: Any) -> Any:
            bodies.append(kwargs.get("json") or {})
            return super().stream(*args, **kwargs)

    return _Client


def test_prefilter_sends_default_medium_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client", _recording_client(_OK_LINES, bodies, [])
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert bodies[0]["reasoning"] == {"summary": "auto", "effort": "medium"}


def test_prefilter_uses_configured_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    config.llm_reasoning_effort = "low"
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client", _recording_client(_OK_LINES, bodies, [])
    )

    semantic_prefilter(transcription, config)

    assert bodies[0]["reasoning"]["effort"] == "low"


def test_prefilter_chat_completions_request_has_no_reasoning(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    config.llm_endpoint = "https://api.example.com/v1/chat/completions"
    bodies: list[dict[str, Any]] = []
    chat_lines = [
        'data: {"choices": [{"delta": {"content": "{\\"points_of_interest\\": []}"}}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client", _recording_client(chat_lines, bodies, [])
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert "reasoning" not in bodies[0]


def _reasoning_delta(text: str) -> str:
    return _event({"type": "response.reasoning_summary_text.delta", "delta": text})


def test_prefilter_failed_after_reasoning_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """The live failure: reasoning deltas, no text, then response.failed with a
    transient-looking server_error -> exactly ONE attempt and an actionable reason."""
    attempts: list[int] = []
    lines = [
        _event({"type": "response.created", "response": {"id": "resp_loop"}}),
        _reasoning_delta("Need maybe include X covers..."),
        _reasoning_delta("Need maybe include X covers..."),
        _event(
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {
                        "code": "server_error",
                        "message": (
                            "The compatible provider rejected the request before it "
                            "could be completed."
                        ),
                    },
                },
            }
        ),
    ]
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client", _sequenced_client([lines], attempts)
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert len(attempts) == 1
    assert "The compatible provider rejected the request" in result.error
    assert "server_error" in result.error
    assert "SCREENSCRIBE_LLM_REASONING_EFFORT=low" in result.error


def test_prefilter_failed_before_any_delta_is_retried(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    """A server_error response.failed before any model output stays transient."""
    attempts: list[int] = []
    failing = [
        _event(
            {
                "type": "response.failed",
                "response": {"error": {"code": "server_error", "message": "Upstream down"}},
            }
        )
    ]
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _sequenced_client([failing, _OK_LINES], attempts),
    )

    result = semantic_prefilter(transcription, config)

    assert result.failed is False
    assert len(attempts) == 2


def test_prefilter_reasoning_only_stream_without_error_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
) -> None:
    lines = [_reasoning_delta("thinking"), "data: [DONE]"]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _sequenced_client([lines], []))

    result = semantic_prefilter(transcription, config)

    assert result.failed is True
    assert "without an answer" in result.error
    assert "SCREENSCRIBE_LLM_REASONING_EFFORT=low" in result.error
