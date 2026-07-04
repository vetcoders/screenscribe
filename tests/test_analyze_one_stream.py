"""Stream-reconciliation regression tests for analyze_one (BH57, BH7).

These tests pin two stream-loop correctness bugs in
``screenscribe.unified.analyze_one.analyze_finding_unified_streaming``:

* BH57 -- the canonical ``response_id`` rides on the same
  ``response.completed`` chunk that carries the final content. When the final
  content was not longer than the already-collected deltas, the loop used to
  ``continue`` past the id extraction, silently dropping the response id.
* BH7  -- ``is_final_text`` is the provider's source-of-truth assertion. A
  final text shorter than the concatenated deltas used to be silently dropped
  instead of reconciled.

Falsify BH57: move the ``response_id`` extraction back below the content
reconciliation (restore the ``continue``-before-extraction order) and
``test_response_id_extracted_even_when_final_not_longer`` goes red.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import analyze_finding_unified_streaming


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self) -> "_FakeStreamResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines


class _LinesClient:
    """httpx.Client stand-in that streams a fixed list of SSE lines."""

    def __init__(self, *args: Any, lines: list[str] | None = None, **kwargs: Any) -> None:
        self._lines = lines or []

    def __enter__(self) -> "_LinesClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._lines)


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )


def _detection() -> Detection:
    return Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    monkeypatch.setattr(
        "screenscribe.unified_analysis.httpx.Client",
        lambda *args, **kwargs: _LinesClient(*args, lines=lines, **kwargs),
    )


def test_response_id_extracted_even_when_final_not_longer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH57: response id from response.completed survives non-longer final content.

    The id is carried ONLY by the terminal response.completed chunk, and that
    chunk's final content equals (is not longer than) the already-collected
    deltas. The old code did `continue` before extracting the id -> id lost.
    """
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    summary_text = "The save button does nothing."
    response_payload = {
        "id": "resp_only_in_completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": summary_text}],
            }
        ],
    }
    lines = [
        # No id on response.created -> the ONLY source of the id is the
        # terminal response.completed chunk below.
        "event: response.created",
        "data: " + json.dumps({"type": "response.created", "response": {}}),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": summary_text}),
        # final content == collected deltas -> not longer -> used to `continue`.
        "data: " + json.dumps({"type": "response.completed", "response": response_payload}),
        "data: [DONE]",
    ]
    _patch_client(monkeypatch, lines)

    result = analyze_finding_unified_streaming(_detection(), screenshot, _config())

    assert result is not None
    assert result.response_id == "resp_only_in_completed"
    assert result.summary == summary_text


def test_is_final_text_shorter_than_deltas_is_honored_not_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH7: a final text shorter than the deltas is honored, not silently dropped.

    response.output_text.done sets is_final_text=True. Even though its text is
    shorter than the accumulated deltas, it is the provider's source of truth
    and must replace the collected content.
    """
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    final_text = "Crash on save."
    lines = [
        "event: response.created",
        "data: "
        + json.dumps({"type": "response.created", "response": {"id": "resp_final_shorter"}}),
        "data: "
        + json.dumps(
            {
                "type": "response.output_text.delta",
                "delta": "A very long partial draft that overshoots the final summary text.",
            }
        ),
        # is_final_text=True and strictly shorter than the delta above.
        "data: " + json.dumps({"type": "response.output_text.done", "text": final_text}),
        "data: [DONE]",
    ]
    assert len(final_text) < len(
        "A very long partial draft that overshoots the final summary text."
    )
    _patch_client(monkeypatch, lines)

    result = analyze_finding_unified_streaming(_detection(), screenshot, _config())

    assert result is not None
    assert result.summary == final_text
    assert result.response_id == "resp_final_shorter"


# --- C6.5: SSE shape robustness -------------------------------------------


def _delta_line(text: str) -> str:
    return "data: " + json.dumps({"type": "response.output_text.delta", "delta": text})


def test_wrong_shape_chunk_skipped_stream_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1/AC2: a valid-JSON but non-dict chunk (a bare number) between two real
    deltas is skipped; the stream survives and both deltas are delivered."""
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    lines = [
        _delta_line("A"),
        "data: 42",  # valid JSON, non-dict -> isinstance guard -> skip
        _delta_line("B"),
        "data: [DONE]",
    ]
    _patch_client(monkeypatch, lines)

    captured: list[str] = []
    result = analyze_finding_unified_streaming(
        _detection(), screenshot, _config(), on_content=captured.append
    )

    # Both valid deltas processed; the malformed one skipped. If the stream had
    # aborted, we'd see only ["A"] then a fallback (which _LinesClient lacks .post
    # for) -- so this exact list proves graceful skip + stream survival.
    assert captured == ["A", "B"]
    assert result is not None


def test_nested_wrong_shape_chunk_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AC3: a dict chunk whose nested shape is wrong ({"choices": [42]} ->
    choices[0].get(...) raises AttributeError) is skipped, not fatal."""
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    lines = [
        _delta_line("A"),
        "data: " + json.dumps({"choices": [42]}),  # nested shape-error
        _delta_line("B"),
        "data: [DONE]",
    ]
    _patch_client(monkeypatch, lines)

    captured: list[str] = []
    result = analyze_finding_unified_streaming(
        _detection(), screenshot, _config(), on_content=captured.append
    )

    assert captured == ["A", "B"]
    assert result is not None


class _FakeEmptyResponse:
    """A non-streaming POST response with empty body -> non-streaming path returns None."""

    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {}


class _ErrorThenDeltaClient:
    """Streams an error-event followed by a delta; .post returns an empty body.

    If the provider error-event were (wrongly) swallowed, the trailing delta
    would build a finding. Correct behavior raises RuntimeError out of the
    stream, which the outer handler turns into a non-streaming fallback that
    here returns None.
    """

    def __init__(self, *args: Any, lines: list[str] | None = None, **kwargs: Any) -> None:
        self._lines = lines or []

    def __enter__(self) -> "_ErrorThenDeltaClient":
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._lines)

    def post(self, *args: Any, **kwargs: Any) -> _FakeEmptyResponse:
        return _FakeEmptyResponse()


def test_provider_error_event_still_aborts_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4: an intentional provider error-event must NOT be swallowed by the new
    shape guard -- it still raises and aborts the stream (no finding is built
    from the content that follows the error)."""
    lines = [
        "data: " + json.dumps({"type": "error", "error": {"message": "boom"}}),
        _delta_line("X"),  # would be collected only if the error were swallowed
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "screenscribe.unified_analysis.httpx.Client",
        lambda *args, **kwargs: _ErrorThenDeltaClient(*args, lines=lines, **kwargs),
    )

    # No screenshot -> text-only backend -> outer-handler fallback goes straight
    # to the non-streaming path, which returns None (empty body). A swallowed
    # error would instead build a finding from "X" (result is not None).
    result = analyze_finding_unified_streaming(_detection(), None, _config())
    assert result is None
