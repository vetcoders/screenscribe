"""Transcript-source routing: normalize/resolve + review pipeline wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli_module
from screenscribe.audio import MissingAudioStreamError
from screenscribe.checkpoint import load_checkpoint
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_pipeline import _transcription_context_id
from screenscribe.semantic_filter import PointOfInterest, SemanticFilterResult
from screenscribe.transcribe_types import Segment, TranscriptionResult
from screenscribe.transcript_sources import (
    AudioTranscriptSource,
    OcrTranscriptSource,
    TranscriptSource,
    audio_quality_allows_ocr_fallback,
    get_transcript_source,
    normalize_transcript_source,
    resolve_transcript_source,
    response_id_can_chain,
)
from screenscribe.unified_analysis import UnifiedFinding

# --- pure routing logic ----------------------------------------------------


def test_normalize_no_audio_folds_to_ocr() -> None:
    assert normalize_transcript_source("auto", no_audio=True) == "ocr"
    assert normalize_transcript_source("ocr", no_audio=True) == "ocr"


def test_normalize_no_audio_conflicts_with_explicit_audio() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        normalize_transcript_source("audio", no_audio=True)


def test_normalize_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="Invalid --transcript-source"):
        normalize_transcript_source("magic")


def test_resolve_auto_prefers_audio_when_track_present() -> None:
    assert resolve_transcript_source("auto", Path("v.mov"), has_audio=lambda _p: True) == "audio"


def test_resolve_auto_falls_back_to_ocr_without_audio() -> None:
    assert resolve_transcript_source("auto", Path("v.mov"), has_audio=lambda _p: False) == "ocr"


def test_resolve_explicit_sources_ignore_probe() -> None:
    def fail_probe(_p: Path) -> bool:
        raise AssertionError("explicit source must not probe the container")

    assert resolve_transcript_source("audio", Path("v.mov"), has_audio=fail_probe) == "audio"
    assert resolve_transcript_source("ocr", Path("v.mov"), has_audio=fail_probe) == "ocr"


def test_source_registry_protocol_conformance() -> None:
    assert isinstance(get_transcript_source("audio"), TranscriptSource)
    assert isinstance(get_transcript_source("ocr"), TranscriptSource)
    assert isinstance(AudioTranscriptSource(), TranscriptSource)
    assert isinstance(OcrTranscriptSource(frame_interval=2.0), TranscriptSource)


def test_response_id_chaining_requires_same_responses_endpoint() -> None:
    endpoint = "https://vision.example/v1/responses"
    assert response_id_can_chain(endpoint, endpoint + "/")
    assert not response_id_can_chain(endpoint, "https://llm.example/v1/responses")
    assert not response_id_can_chain(endpoint, "https://vision.example/v1/chat/completions")


def test_ocr_context_id_is_dropped_for_split_provider() -> None:
    config = ScreenScribeConfig(
        vision_endpoint="https://vision.example/v1/responses",
        llm_endpoint="https://llm.example/v1/responses",
    )

    assert (
        _transcription_context_id(
            "ocr-response",
            "batch-response",
            source="ocr",
            config=config,
        )
        == "batch-response"
    )


def test_ocr_context_id_is_kept_for_same_responses_endpoint() -> None:
    endpoint = "https://same.example/v1/responses"
    config = ScreenScribeConfig(vision_endpoint=endpoint, llm_endpoint=endpoint)

    assert (
        _transcription_context_id(
            "ocr-response",
            "batch-response",
            source="ocr",
            config=config,
        )
        == "ocr-response"
    )


def test_context_id_is_dropped_for_chat_completions_endpoint() -> None:
    config = ScreenScribeConfig(
        vision_endpoint="https://same.example/v1/responses",
        llm_endpoint="https://same.example/v1/chat/completions",
    )

    assert (
        _transcription_context_id(
            "ocr-response",
            "batch-response",
            source="ocr",
            config=config,
        )
        == ""
    )


# --- review wiring ----------------------------------------------------------


def _ocr_transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="Przycisk Zapisz nie reaguje",
        segments=[
            Segment(id=0, start=0.0, end=5.0, text="Przycisk Zapisz nie reaguje"),
        ],
        language="pl",
        response_id="ocr-resp-1",
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _p: 12.0)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),  # pragma: allowlist secret
    )


def test_no_audio_routes_to_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A silent recording with --no-audio reaches semantic analysis via OCR
    instead of dying at the 'has no audio track' gate."""
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "silent_review"

    captured: dict[str, object] = {}
    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)

    def fail_audio_gate(_video: Path) -> None:
        raise AssertionError("audio gate must not run on the OCR route")

    monkeypatch.setattr("screenscribe.cli._require_audio_or_exit", fail_audio_gate)

    def fake_ocr(video_path: Path, config: ScreenScribeConfig, *, frame_interval: float = 5.0):
        captured["ocr_video"] = video_path
        captured["frame_interval"] = frame_interval
        return _ocr_transcription()

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fake_ocr)

    def fake_prefilter(transcription, config, **kwargs):
        captured["transcription"] = transcription
        return SemanticFilterResult(pois=[], response_id="filter-resp")

    monkeypatch.setattr("screenscribe.cli.semantic_prefilter", fake_prefilter)

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve", "--no-audio"],
    )
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "has no audio track" not in normalized
    assert captured["ocr_video"] == video
    assert captured["frame_interval"] == 5.0
    # The semantic prefilter consumed the OCR segments (STT-shaped contract).
    transcription = captured["transcription"]
    assert [(s.start, s.end, s.text) for s in transcription.segments] == [
        (0.0, 5.0, "Przycisk Zapisz nie reaguje")
    ]
    report_path = output_dir / "silent_report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["transcript_source"] == "ocr"


def test_auto_without_audio_routes_to_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default 'auto' source probes the container and picks OCR when silent."""
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "silent_review"

    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)
    ocr_called: list[Path] = []
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_video_ocr",
        lambda video_path, config, *, frame_interval=5.0: (
            ocr_called.append(video_path) or _ocr_transcription()
        ),
    )
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(pois=[]),
    )

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve"],
    )

    assert result.exit_code == 0, result.output
    assert ocr_called == [video]


def test_ocr_coverage_gaps_warn_on_console_and_in_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A/P2: ranges frame OCR never read must be visible, not silently missing.

    Falsification: drop the pipeline notice and the report's ``errors`` channel
    stays empty while the review claims to cover the whole recording.
    """
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "silent_review"

    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)
    gapped = _ocr_transcription()
    gapped.coverage_gaps = [(65.0, 70.0), (125.0, 130.0)]
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_video_ocr",
        lambda video_path, config, **kwargs: gapped,
    )
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(pois=[]),
    )

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve", "--no-audio"],
    )
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "OCR Coverage Warning" in normalized
    assert "01:05-01:10" in normalized

    report = json.loads((output_dir / "silent_report.json").read_text(encoding="utf-8"))
    (gap_error,) = [e for e in report["errors"] if e["stage"] == "transcription"]
    assert "01:05-01:10" in gap_error["message"]
    assert "02:05-02:10" in gap_error["message"]
    assert "incomplete" in gap_error["message"]


def test_complete_ocr_run_adds_no_coverage_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "silent_review"

    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_video_ocr",
        lambda video_path, config, **kwargs: _ocr_transcription(),
    )
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(pois=[]),
    )

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve", "--no-audio"],
    )

    assert result.exit_code == 0, result.output
    assert "OCR Coverage Warning" not in " ".join(result.output.split())
    report = json.loads((output_dir / "silent_report.json").read_text(encoding="utf-8"))
    assert report["errors"] == []


def _speechless_stt_transcription() -> TranscriptionResult:
    """STT output ``validate_audio_quality`` rejects as a silence hallucination."""
    return TranscriptionResult(
        text="Thank you for watching",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="Thank you for watching", no_speech_prob=0.4),
        ],
        language="en",
        response_id="stt-resp-1",
    )


def _patch_audio_stream(monkeypatch: pytest.MonkeyPatch, audio_path: Path) -> None:
    """Route the pipeline down the audio source: a real stream, extractable."""
    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: True)
    monkeypatch.setattr("screenscribe.cli.require_audio_stream", lambda _p: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _p: audio_path)


def test_auto_falls_back_to_ocr_when_the_audio_carries_no_speech(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B/P2: an audio stream is not proof of speech.

    Falsification: keep the old unconditional stop and the run dies with
    "Processing stopped" instead of reviewing a recording whose content is
    entirely on screen.
    """
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "screencast.mov"
    video.write_bytes(b"video")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    output_dir = tmp_path / "screencast_review"
    _patch_audio_stream(monkeypatch, audio)

    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio_chunked",
        lambda *a, **kw: _speechless_stt_transcription(),
    )
    ocr_called: list[Path] = []
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_video_ocr",
        lambda video_path, config, **kwargs: ocr_called.append(video_path) or _ocr_transcription(),
    )
    seen: dict[str, object] = {}

    def fake_prefilter(transcription, config, **kwargs):
        seen["transcription"] = transcription
        return SemanticFilterResult(pois=[])

    monkeypatch.setattr("screenscribe.cli.semantic_prefilter", fake_prefilter)

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve"],
    )
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Processing stopped" not in normalized
    assert "audio stream carries no speech, switching to frame OCR" in normalized
    assert ocr_called == [video]
    # Downstream analysis consumed the OCR segments, not the hallucination.
    assert [s.text for s in seen["transcription"].segments] == ["Przycisk Zapisz nie reaguje"]
    report = json.loads((output_dir / "screencast_report.json").read_text(encoding="utf-8"))
    assert report["transcript_source"] == "ocr"


def test_explicit_audio_source_still_stops_on_speechless_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--transcript-source audio is a decision, not a preference: keep stopping."""
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "screencast.mov"
    video.write_bytes(b"video")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _patch_audio_stream(monkeypatch, audio)

    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio_chunked",
        lambda *a, **kw: _speechless_stt_transcription(),
    )

    def fail_ocr(*args: object, **kwargs: object) -> None:
        raise AssertionError("explicit audio source must not fall back to OCR")

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fail_ocr)

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video),
            "-o",
            str(tmp_path / "screencast_review"),
            "--no-serve",
            "--transcript-source",
            "audio",
        ],
    )
    normalized = " ".join(result.output.split())

    assert "Audio Quality Issue" in normalized
    assert "Processing stopped" in normalized


def test_auto_does_not_fall_back_for_empty_or_corrupt_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No segments at all is a broken input, not a speechless stream: stop."""
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "screencast.mov"
    video.write_bytes(b"video")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _patch_audio_stream(monkeypatch, audio)

    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio_chunked",
        lambda *a, **kw: TranscriptionResult(text="", segments=[], language="en"),
    )

    def fail_ocr(*args: object, **kwargs: object) -> None:
        raise AssertionError("corrupt audio must not be papered over with OCR")

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fail_ocr)

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(tmp_path / "screencast_review"), "--no-serve"],
    )
    normalized = " ".join(result.output.split())

    assert "Audio Quality Issue" in normalized
    assert "Processing stopped" in normalized


def _two_segment_ocr_transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="Przycisk Zapisz nie reaguje\nBlad: quota",
        segments=[
            Segment(id=0, start=0.0, end=5.0, text="Przycisk Zapisz nie reaguje"),
            Segment(id=1, start=5.0, end=10.0, text="Blad: quota"),
        ],
        language="pl",
        response_id="ocr-resp-1",
    )


def _unified_finding_for(detection: object) -> UnifiedFinding:
    segment = detection.segment  # type: ignore[attr-defined]
    return UnifiedFinding(
        detection_id=segment.id,
        screenshot_path=None,
        timestamp=segment.start,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity="high",
        summary=f"issue {segment.id} on screen",
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp",
        merged_from_ids=[],
    )


def test_auto_ocr_fallback_survives_resume_without_rerunning_stt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mid-run switch is part of the resumable contract.

    Re-resolution on the next run only sees the audio *stream* and says
    "audio" again. Without adopting the persisted OCR contract the checkpoint
    is thrown away and the whole speechless STT round is paid for twice.
    """
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "screencast.mov"
    video.write_bytes(b"video")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    output_dir = tmp_path / "screencast_review"
    _patch_audio_stream(monkeypatch, audio)

    stt_calls: list[int] = []

    def counted_stt(*_a: object, **_kw: object) -> TranscriptionResult:
        stt_calls.append(1)
        return _speechless_stt_transcription()

    monkeypatch.setattr("screenscribe.cli.transcribe_audio_chunked", counted_stt)
    ocr_calls: list[int] = []

    def counted_ocr(video_path: Path, config: ScreenScribeConfig, **_kw: object):
        ocr_calls.append(1)
        return _two_segment_ocr_transcription()

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", counted_ocr)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(
            pois=[
                PointOfInterest(
                    timestamp_start=float(index * 5),
                    timestamp_end=float(index * 5 + 5),
                    category="bug",
                    confidence=0.9,
                    reasoning=f"issue {index}",
                    transcript_excerpt=f"issue {index}",
                    segment_ids=[index],
                )
                for index in range(2)
            ]
        ),
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda _video, detections, _dir: [
            (d, tmp_path / f"shot-{d.segment.id}.jpg") for d in detections
        ],
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_unified_summary", lambda *a, **kw: "summary"
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_visual_summary_unified", lambda *a, **kw: "visual"
    )
    unified_batch_sizes: list[int] = []

    def partial_then_complete(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        unified_batch_sizes.append(len(screenshots))
        if len(unified_batch_sizes) == 1:
            return [_unified_finding_for(d) for (d, _p) in screenshots[:-1]]
        return [_unified_finding_for(d) for (d, _p) in screenshots]

    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified", partial_then_complete
    )

    first = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve"],
    )
    assert first.exit_code == 0, first.output
    assert "switching to frame OCR" in " ".join(first.output.split())
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None, "a partial run must keep the checkpoint"
    # The switch is persisted, so the resumed run knows which source produced it.
    assert checkpoint.analysis_inputs["transcript_source"] == "ocr"
    assert checkpoint.analysis_inputs["frame_interval"] == 5.0
    assert checkpoint.is_stage_complete("transcription")

    second = runner.invoke(
        cli_module.app,
        ["review", str(video), "-o", str(output_dir), "--no-serve", "--resume"],
    )
    normalized = " ".join(second.output.split())

    assert second.exit_code == 0, second.output
    assert "Resuming from checkpoint" in normalized
    assert "No valid checkpoint found" not in normalized
    # Neither transcription stage ran again; only the residual VLM item did.
    assert stt_calls == [1]
    assert ocr_calls == [1]
    assert unified_batch_sizes == [2, 1]
    report = json.loads((output_dir / "screencast_report.json").read_text(encoding="utf-8"))
    assert report["transcript_source"] == "ocr"


def test_audio_quality_fallback_classifier_separates_silence_from_corruption() -> None:
    assert audio_quality_allows_ocr_fallback(_speechless_stt_transcription()) is True
    assert (
        audio_quality_allows_ocr_fallback(TranscriptionResult(text="", segments=[], language="en"))
        is False
    )


def test_transcript_source_audio_keeps_readable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit --transcript-source audio on a silent file keeps today's error."""
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")

    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)
    monkeypatch.setattr(
        "screenscribe.cli.require_audio_stream",
        lambda path: (_ for _ in ()).throw(
            MissingAudioStreamError(f"Video '{path.name}' has no audio track.")
        ),
    )

    def fail_ocr(*args: object, **kwargs: object) -> None:
        raise AssertionError("OCR must not run for explicit audio source")

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fail_ocr)

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "--no-serve", "--transcript-source", "audio"],
    )
    normalized = " ".join(result.output.split())

    assert result.exit_code == 1
    assert "has no audio track" in normalized


def test_prompt_override_reaches_ocr_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--prompt lands on config.analysis_prompt_override for the OCR stage and
    the semantic stage alike."""
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "silent_review"

    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)
    seen: dict[str, str] = {}

    def fake_ocr(video_path: Path, config: ScreenScribeConfig, *, frame_interval: float = 5.0):
        seen["ocr_override"] = config.analysis_prompt_override
        return _ocr_transcription()

    def fake_prefilter(transcription, config, **kwargs):
        seen["semantic_override"] = config.analysis_prompt_override
        return SemanticFilterResult(pois=[])

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fake_ocr)
    monkeypatch.setattr("screenscribe.cli.semantic_prefilter", fake_prefilter)

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video),
            "-o",
            str(output_dir),
            "--no-serve",
            "--no-audio",
            "--prompt",
            "Nagranie bez audio; opisy sa w tekscie na ekranie",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["ocr_override"] == "Nagranie bez audio; opisy sa w tekscie na ekranie"
    assert seen["semantic_override"] == "Nagranie bez audio; opisy sa w tekscie na ekranie"


def test_frame_interval_flag_reaches_ocr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "silent.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "silent_review"

    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)
    seen: dict[str, float] = {}

    def fake_ocr(video_path: Path, config: ScreenScribeConfig, *, frame_interval: float = 5.0):
        seen["frame_interval"] = frame_interval
        return _ocr_transcription()

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fake_ocr)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(pois=[]),
    )

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video),
            "-o",
            str(output_dir),
            "--no-serve",
            "--transcript-source",
            "ocr",
            "--frame-interval",
            "2.5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["frame_interval"] == 2.5


def test_no_audio_conflicts_with_explicit_audio_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "--no-serve", "--no-audio", "--transcript-source", "audio"],
    )

    assert result.exit_code == 2
    assert "conflicts" in result.output


def test_invalid_transcript_source_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_common(monkeypatch)
    runner = CliRunner()
    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")

    result = runner.invoke(
        cli_module.app,
        ["review", str(video), "--no-serve", "--transcript-source", "magic"],
    )

    assert result.exit_code == 2
    assert "Invalid --transcript-source" in result.output


def test_review_help_lists_transcript_source_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["review", "--help"])

    assert result.exit_code == 0
    for flag in ("--transcript-source", "--no-audio", "--frame-interval"):
        assert flag in result.output
