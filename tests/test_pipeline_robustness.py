"""Pipeline-robustness regression tests (W1-C).

Each test pins one hardening from the quality recon: the pipeline degrades a
single bad element (a null API field, a wedged ffmpeg, a torn connection, an
odd-shaped stream chunk) per-element with a log, instead of throwing an
uncaught exception that takes down the whole run.

Grouped by finding:
  1. no_speech_prob coercion (transcribe)
  2. ffmpeg/ffprobe timeouts (audio)
  3. httpx transport errors are retriable (api_utils)
  4. malformed prefilter stream chunk is skipped, not fatal (semantic_filter)
  5. null/non-dict POI shapes are filtered per-element (semantic_filter)
  6. streaming VLM retries 429/5xx before degrading to text-only (analyze_one)
  7. extract_audio uses a unique, cleaned-up temp path (audio)
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx
import pytest

from screenscribe import audio
from screenscribe.api_utils import is_retriable_error, retry_request
from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.semantic_filter import (
    PointOfInterest,
    _parse_prefilter_response,
    deduplicate_pois,
    semantic_prefilter,
)
from screenscribe.transcribe import (
    Segment,
    TranscriptionResult,
    _build_transcription_result,
    filter_hallucinated_segments,
    validate_audio_quality,
)
from screenscribe.unified_analysis import analyze_finding_unified_streaming

# --- Finding 1: no_speech_prob coercion -----------------------------------


def test_no_speech_prob_null_and_str_do_not_crash_downstream() -> None:
    """A segment carrying no_speech_prob=null or a numeric string must be coerced
    to a real float at build time, so the float-math consumers
    (validate_audio_quality's sum(), filter_hallucinated_segments' comparison)
    never hit ``None > 0.6`` / ``sum(None, ...)`` TypeErrors."""
    payload = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "hello", "no_speech_prob": None},
            {"start": 1.0, "end": 2.0, "text": "world", "no_speech_prob": "0.3"},
        ]
    }

    result = _build_transcription_result(payload, "en")

    assert all(isinstance(seg.no_speech_prob, float) for seg in result.segments)
    assert result.segments[0].no_speech_prob == 0.0  # null -> safe fallback
    assert result.segments[1].no_speech_prob == 0.3  # "0.3" -> 0.3

    # The two downstream float consumers must run without raising.
    validate_audio_quality(result)
    filter_hallucinated_segments(result)


# --- Finding 2: ffmpeg/ffprobe timeouts -----------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ffprobe_duration_passes_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_audio_duration must pass a positive timeout= to subprocess.run."""
    seen: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        seen.append(kwargs)
        return _FakeCompleted(returncode=0, stdout="12.5")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    assert audio.get_audio_duration(tmp_path / "a.mp3") == 12.5
    assert seen and seen[0].get("timeout", 0) > 0


def test_get_audio_duration_timeout_is_readable_runtimeerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wedged ffprobe (TimeoutExpired) becomes a readable RuntimeError, not an
    uncaught TimeoutExpired traceback or an infinite hang."""

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        audio.get_audio_duration(tmp_path / "a.mp3")


def test_transcode_timeout_is_readable_runtimeerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wedged ffmpeg transcode becomes a readable RuntimeError, not a hang."""
    inp = tmp_path / "in.mp4"
    inp.write_bytes(b"x")
    out = tmp_path / "out.mp3"

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        assert kwargs.get("timeout", 0) > 0  # timeout is actually wired
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        audio._transcode_input_to_mp3(inp, out)


def test_tail_is_silent_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tail_is_silent already treats an unmeasurable slice as None; a timeout
    (a SubprocessError subclass) must fall into that same 'cannot measure' path
    rather than escaping as an exception."""
    audio_file = tmp_path / "a.mp3"
    audio_file.write_bytes(b"x")

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    assert audio.tail_is_silent(audio_file, 0.0, 5.0) is None


# --- Finding 3: httpx transport errors are retriable ----------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("read reset"),
        httpx.WriteError("write reset"),
        httpx.NetworkError("network drop"),
        httpx.ConnectError("connection refused"),
    ],
)
def test_transport_errors_are_retriable(exc: Exception) -> None:
    """Mid-transfer transport drops (ReadError/WriteError/RemoteProtocolError,
    plus the NetworkError base and ConnectError) are transient and retriable."""
    assert is_retriable_error(exc) is True


def test_local_protocol_error_is_not_retriable() -> None:
    """A client-side LocalProtocolError is a bug, not a transient drop, so it
    stays non-retriable (it is a ProtocolError but NOT a RemoteProtocolError)."""
    assert is_retriable_error(httpx.LocalProtocolError("bad request line")) is False


def test_retry_request_retries_read_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single ReadError (mid-upload connection reset) is now retried instead of
    propagating immediately and killing the whole transcription."""
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda _d: None)
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("connection reset mid-upload")
        return "ok"

    assert retry_request(fn, max_retries=3, operation_name="STT") == "ok"
    assert calls["n"] == 2


# --- Finding 4: malformed prefilter stream chunk is skipped, not fatal -----


def _tr() -> TranscriptionResult:
    return TranscriptionResult(
        text="The save button does nothing.",
        segments=[Segment(id=0, start=0.0, end=4.0, text="The save button does nothing.")],
        language="en",
    )


def _config_llm() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        language="en",
    )


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


def _lines_client(lines: list[str]) -> type:
    """An httpx.Client stand-in that streams a fixed list of SSE lines (200 OK)."""

    class _Stream:
        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self) -> Iterator[str]:
            yield from lines

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


def test_prefilter_survives_malformed_shape_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid-JSON but non-dict chunk (``data: [1,2,3]``) between real deltas is
    skipped; the fully-streamed prefilter still succeeds and parses its POI,
    instead of turning into a hard stage failure that skips the whole video."""
    lines = [
        _OK_POI_DELTA,
        "data: [1, 2, 3]",  # valid JSON, non-dict -> must be skipped, not fatal
        "data: [DONE]",
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _lines_client(lines))

    result = semantic_prefilter(_tr(), _config_llm())

    assert result.failed is False
    assert len(result.pois) == 1


# --- Finding 5: null / non-dict POI shapes filtered per-element ------------


def test_parse_null_poi_list_returns_empty() -> None:
    """{"points_of_interest": null} (a benign 'found nothing') degrades to [],
    not a ``for item in None`` TypeError that aborts the stage."""
    assert _parse_prefilter_response('{"points_of_interest": null}', _tr()) == []


def test_parse_skips_non_dict_items_and_coerces_null_strings() -> None:
    """A non-dict list entry is skipped; a present-but-null string field is
    coerced to '' so the returned POI is well-typed."""
    content = json.dumps(
        {
            "points_of_interest": [
                "not-a-dict",
                {
                    "timestamp_start": 1.0,
                    "timestamp_end": 2.0,
                    "category": "bug",
                    "confidence": 0.8,
                    "reasoning": None,
                    "transcript_excerpt": "x",
                },
            ]
        }
    )

    pois = _parse_prefilter_response(content, _tr())

    assert len(pois) == 1
    assert pois[0].reasoning == ""  # null coerced to empty string
    assert pois[0].transcript_excerpt == "x"


def test_deduplicate_pois_tolerates_null_excerpt() -> None:
    """Two similar POIs whose transcript_excerpt is None must merge without an
    ``None.strip()`` AttributeError mid-video."""
    shared_reason = "the save button does nothing when clicked"
    a = PointOfInterest(1.0, 2.0, "bug", 0.8, shared_reason, None)  # type: ignore[arg-type]
    b = PointOfInterest(1.0, 2.0, "bug", 0.9, shared_reason, None)  # type: ignore[arg-type]

    merged = deduplicate_pois([a, b])

    assert len(merged) == 1
    assert isinstance(merged[0].transcript_excerpt, str)


# --- Finding 6: streaming VLM retries 429/5xx before text-only degrade -----


def _config_vision() -> ScreenScribeConfig:
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
        segment=Segment(id=1, start=12.5, end=15.0, text="The next button does not work."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="User reports a broken button on the config screen.",
    )


def _http_status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/responses")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError(f"status {status}", request=request, response=response)


class _StreamOutcome:
    """A streamed response whose raise_for_status raises (failure) or streams lines."""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def __enter__(self) -> _StreamOutcome:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def raise_for_status(self) -> None:
        if isinstance(self._outcome, Exception):
            raise self._outcome

    def iter_lines(self) -> Iterator[str]:
        if isinstance(self._outcome, Exception):
            return iter([])
        return iter(self._outcome)


def _sequenced_client(outcomes: list[Any], attempts: list[int]) -> type:
    """httpx.Client stand-in: the i-th stream() attempt uses ``outcomes[i]``."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _StreamOutcome:
            idx = len(attempts)
            attempts.append(idx)
            return _StreamOutcome(outcomes[min(idx, len(outcomes) - 1)])

    return _Client


def test_streaming_vlm_retries_429_honoring_retry_after_no_text_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient 429 with Retry-After on the image-backed streaming call is
    retried (honoring the header) and succeeds on the 2nd attempt WITH the image,
    never degrading to the text-only fallback."""
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    summary = "The button is misaligned and unclickable."
    good_lines = [
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": summary}),
        "data: " + json.dumps({"type": "response.completed", "response": {"id": "resp_ok"}}),
        "data: [DONE]",
    ]

    slept: list[float] = []
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda d: slept.append(d))

    attempts: list[int] = []
    monkeypatch.setattr(
        "screenscribe.unified.analyze_one.httpx.Client",
        _sequenced_client([_http_status_error(429, {"Retry-After": "0"}), good_lines], attempts),
    )

    result = analyze_finding_unified_streaming(_detection(), screenshot, _config_vision())

    assert result is not None
    assert result.summary == summary
    assert len(attempts) == 2  # one 429, recovered on the retry (not text-only)
    assert slept == [0.0]  # honored Retry-After, not exponential backoff


# --- Finding 7: extract_audio unique + cleaned-up temp path ---------------


def test_extract_audio_gives_unique_paths_for_same_stem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two extractions of the same stem must not collide on one predictable temp
    path (the concurrent-clobber the split_audio_chunks fix already closed)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    monkeypatch.setattr(audio, "require_audio_stream", lambda _p: None)

    def fake_transcode(inp: Path, out: Path) -> Path:
        Path(out).write_bytes(b"mp3")
        return out

    monkeypatch.setattr(audio, "_transcode_input_to_mp3", fake_transcode)

    p1 = audio.extract_audio(video)
    p2 = audio.extract_audio(video)
    try:
        assert p1 != p2
        assert p1.exists() and p2.exists()
    finally:
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)


def test_extract_audio_cleans_temp_on_transcode_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transcode failure must not leak the temp placeholder we created."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    monkeypatch.setattr(audio, "require_audio_stream", lambda _p: None)

    created: list[Path] = []

    def boom(inp: Path, out: Path) -> Path:
        created.append(Path(out))
        raise RuntimeError("transcode failed")

    monkeypatch.setattr(audio, "_transcode_input_to_mp3", boom)

    with pytest.raises(RuntimeError, match="transcode failed"):
        audio.extract_audio(video)

    assert created and not created[0].exists()  # placeholder cleaned on error path
