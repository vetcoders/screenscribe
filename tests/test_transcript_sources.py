"""Transcript-source routing: normalize/resolve + review pipeline wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli_module
from screenscribe.audio import MissingAudioStreamError
from screenscribe.config import ScreenScribeConfig
from screenscribe.semantic_filter import SemanticFilterResult
from screenscribe.transcribe_types import Segment, TranscriptionResult
from screenscribe.transcript_sources import (
    AudioTranscriptSource,
    OcrTranscriptSource,
    TranscriptSource,
    get_transcript_source,
    normalize_transcript_source,
    resolve_transcript_source,
)

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
    assert (output_dir / "silent_report.json").exists()


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
