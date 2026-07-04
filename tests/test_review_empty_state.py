"""Regression tests for the review command's empty-state path.

When the semantic pre-filter returns 0 points of interest (e.g. on a short
video with generic speech like "OK, success."), the pipeline must still
produce all three report artifacts (JSON, Markdown, HTML) instead of
silently exiting with an empty review directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

import screenscribe.cli as cli_module
from screenscribe.audio import MissingAudioStreamError
from screenscribe.checkpoint import load_checkpoint
from screenscribe.config import ScreenScribeConfig
from screenscribe.semantic_filter import PointOfInterest, SemanticFilterResult
from screenscribe.transcribe import Segment, TranscriptionResult


def test_review_empty_state_uses_single_cli_import_style() -> None:
    """Keep this file on one CLI import path so review comments stay quiet."""
    assert cli_module.app is not None


def _empty_state_transcription() -> TranscriptionResult:
    """Single short segment with generic content the prefilter would skip."""
    return TranscriptionResult(
        text="OK, success.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="OK, success.", no_speech_prob=0.05),
        ],
        language="en",
        response_id="resp_stt_test_empty",
    )


def _compressed_timeline_transcription() -> TranscriptionResult:
    """Synthetic long-video drift fixture: transcript stops far before video end."""
    return TranscriptionResult(
        text="This transcript appears complete, but its timestamps stop too early.",
        segments=[
            Segment(
                id=0,
                start=0.0,
                end=284.0,
                text="This transcript appears complete, but its timestamps stop too early.",
                no_speech_prob=0.02,
            ),
        ],
        language="en",
        response_id="resp_stt_test_compressed",
    )


def test_review_writes_all_three_reports_when_prefilter_returns_zero_pois(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: 0 POIs from prefilter must still produce JSON+MD+HTML reports.

    Reproduces the bug where a short screencast with one generic transcript
    segment passed STT and prefilter cleanly but produced an empty review
    directory because the pipeline `continue`d before any report was written.
    """
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")

    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")

    output_dir = tmp_path / "demo_review"

    # Stub heavy dependencies so the pipeline runs end-to-end without FFmpeg
    # / network / models.
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _empty_state_transcription(),
    )
    # Skip model availability probe (no API calls in the test).
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    # The semantic prefilter is the unit under test for this scenario:
    # it must return zero POIs to trigger the empty-state branch.
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: SemanticFilterResult(pois=[], response_id="resp_filter_empty"),
    )
    # Avoid any real config / API key requirements; provide a dummy key so
    # config.validate() passes for both LLM and vision endpoints.
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            "-o",
            str(output_dir),
            "--no-serve",
            "--skip-validation",
        ],
    )

    assert result.exit_code == 0, result.output

    json_path = output_dir / "demo_report.json"
    md_path = output_dir / "demo_report.md"
    html_path = output_dir / "demo_report.html"

    assert json_path.exists(), f"JSON report missing. CLI output:\n{result.output}"
    assert md_path.exists(), f"Markdown report missing. CLI output:\n{result.output}"
    assert html_path.exists(), f"HTML report missing. CLI output:\n{result.output}"

    # JSON report must be well-formed and report 0 findings + the embedded transcript.
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["summary"]["total"] == 0
    assert report["findings"] == []
    assert report["transcript"] == "OK, success."
    assert "executive_summary" in report
    assert "no points of interest" in report["executive_summary"].lower()

    # Markdown report must embed the transcript and the empty-state summary.
    md_text = md_path.read_text(encoding="utf-8")
    assert "OK, success." in md_text
    assert "no points of interest" in md_text.lower()

    # HTML report must include the empty-state summary text.
    html_text = html_path.read_text(encoding="utf-8")
    assert "no points of interest" in html_text.lower()


def _run_review_with_failed_prefilter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[object, Path]:
    """Drive `review` where the semantic pre-filter reports a hard FAILURE.

    Mirrors the empty-state harness, but the pre-filter returns
    ``failed=True`` (e.g. a 401/429/network drop) instead of a genuine
    zero-POI success. The pipeline must distinguish the two.
    """
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _empty_state_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: SemanticFilterResult(
            pois=[], response_id="", failed=True, error="HTTP 401 Unauthorized"
        ),
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )
    return result, output_dir


def test_review_failed_prefilter_does_not_write_a_false_no_issues_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed pre-filter must NOT emit a confident "no issues detected" report."""
    result, output_dir = _run_review_with_failed_prefilter(monkeypatch, tmp_path)
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    # The dangerous failure: a clean-looking deliverable on a broken LLM.
    assert not (output_dir / "demo_report.html").exists()
    assert not (output_dir / "demo_report.json").exists()
    assert not (output_dir / "demo_report.md").exists()
    # Instead the user gets an actionable failure + retry guidance.
    assert "Issue Detection Failed" in normalized_output
    assert "--resume" in normalized_output
    assert "no points of interest" not in normalized_output.lower()
    assert "no issues detected" not in normalized_output.lower()
    assert "Traceback" not in result.output


def test_review_failed_prefilter_is_not_checkpointed_as_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed pre-filter must not checkpoint detection, so --resume genuinely retries."""
    result, output_dir = _run_review_with_failed_prefilter(monkeypatch, tmp_path)

    assert result.exit_code == 0, result.output
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None, "checkpoint must be kept so --resume can retry"
    assert checkpoint.is_stage_complete("transcription")
    assert not checkpoint.is_stage_complete("detection")


def test_review_dry_run_writes_no_reports_on_empty_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`review --dry-run` on the zero-detection path must not write any artifacts."""
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _empty_state_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: SemanticFilterResult(pois=[], response_id="resp_filter_empty"),
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            "-o",
            str(output_dir),
            "--no-serve",
            "--skip-validation",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Dry Run Results" in result.output
    assert not (output_dir / "demo_report.json").exists()
    assert not (output_dir / "demo_report.md").exists()
    assert not (output_dir / "demo_report.html").exists()


def test_report_artifact_messages_respect_flags_and_html_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Final CLI output must not claim artifacts disabled by --no-* flags."""
    recorded_console = Console(record=True, width=100)
    monkeypatch.setattr(cli_module, "console", recorded_console)

    cli_module._print_report_artifact_paths(
        video_output=tmp_path,
        video_stem="demo",
        json_report=False,
        markdown_report=False,
        html_report=True,
    )

    output = recorded_console.export_text()
    assert "Enhanced report saved" not in output
    assert "Enhanced Markdown report saved" not in output
    assert "Interactive HTML report saved" in output


def test_review_reports_friendly_message_on_stt_429(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 429 from STT must produce an actionable panel, not a raw traceback."""
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    def raise_429(*_: object, **__: object) -> TranscriptionResult:
        request = httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")
        response = httpx.Response(
            429,
            headers={"retry-after": "5"},
            json={"error": "Too Many Requests", "message": "Maximum 10 concurrent requests."},
            request=request,
        )
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr("screenscribe.cli.transcribe_audio", raise_429)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Transcription Failed" in normalized_output
    assert "rate-limited or at capacity" in normalized_output
    # Server-provided detail is surfaced (single word survives panel word-wrap).
    assert "concurrent" in normalized_output
    assert "--resume" in normalized_output
    # No traceback leaked to the user.
    assert "Traceback" not in result.output
    assert "HTTPStatusError" not in result.output


def test_review_handles_stt_runtime_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A RuntimeError from STT (unexpected payload shape, transcribe.py) must be
    caught by the per-video guard and produce the friendly panel + kept
    checkpoint, not an uncaught traceback. BH47: the except tuple previously
    omitted RuntimeError so this crashed the whole run."""
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    def raise_runtime(*_: object, **__: object) -> TranscriptionResult:
        raise RuntimeError("STT API returned unexpected payload shape")

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr("screenscribe.cli.transcribe_audio", raise_runtime)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Transcription Failed" in normalized_output
    assert "--resume" in normalized_output
    assert "Traceback" not in result.output
    # Checkpoint is kept so --resume can retry without re-extracting audio.
    assert load_checkpoint(output_dir) is not None


def _raise_stt_429(endpoint: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", endpoint)
    response = httpx.Response(429, headers={"retry-after": "5"}, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def test_review_uses_stt_fallback_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Primary STT 429 + a configured fallback must transcribe via the fallback."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    primary = "https://primary.example/v1/audio/transcriptions"
    fallback = "https://fallback.example/v1/audio/transcriptions"

    def fake_transcribe(*_: object, **kwargs: object) -> TranscriptionResult:
        if kwargs.get("stt_endpoint") == primary:
            raise _raise_stt_429(primary)
        return _empty_state_transcription()  # fallback endpoint succeeds

    cfg = ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        stt_endpoint=primary,
        stt_fallback_endpoint=fallback,
        stt_fallback_api_key="sk-fallback",  # pragma: allowlist secret
    )
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr("screenscribe.cli.transcribe_audio", fake_transcribe)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: SemanticFilterResult(pois=[], response_id="resp_filter_empty"),
    )
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: cfg))

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "trying configured fallback endpoint" in normalized_output
    assert "Transcription Failed" not in normalized_output
    assert (output_dir / "demo_report.html").exists()


def test_review_reports_failure_when_primary_and_fallback_both_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If both primary and fallback STT fail, the user gets the friendly panel."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    def always_429(*_: object, **kwargs: object) -> TranscriptionResult:
        raise _raise_stt_429(str(kwargs.get("stt_endpoint")))

    cfg = ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://primary.example/v1/audio/transcriptions",
        stt_fallback_endpoint="https://fallback.example/v1/audio/transcriptions",
        stt_fallback_api_key="sk-fallback",  # pragma: allowlist secret
    )
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr("screenscribe.cli.transcribe_audio", always_429)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: cfg))

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "trying configured fallback endpoint" in normalized_output
    assert "Transcription Failed" in normalized_output
    assert "Traceback" not in result.output


def test_review_no_audio_exits_before_model_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Silent videos should fail before touching model/API validation."""
    runner = CliRunner()

    video_path = tmp_path / "silent.mov"
    video_path.write_bytes(b"video")

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(
        "screenscribe.cli.require_audio_stream",
        lambda path: (_ for _ in ()).throw(
            MissingAudioStreamError(
                f"Video '{path.name}' has no audio track. "
                "The 'review' and 'transcribe' commands require audio for transcription. "
                f"Tip: use 'screenscribe analyze {path}' for interactive vision-only review "
                "(mark frames manually, add optional text/voice notes)."
            )
        ),
    )
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 1.0)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),  # pragma: allowlist secret
    )

    def fail_if_called(*_: object, **__: object) -> None:
        raise AssertionError("validation should not run")

    monkeypatch.setattr("screenscribe.cli.validate_models", fail_if_called)

    result = runner.invoke(cli_module.app, ["review", str(video_path), "--no-serve"])
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 1
    assert "has no audio track" in normalized_output
    assert "screenscribe analyze" in normalized_output
    assert "validation should not run" not in normalized_output


def _run_low_coverage_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tail_silent: bool | None,
) -> object:
    """Drive `review` on a long video whose transcript stops far before the end.

    Coverage is 284/502 ≈ 57% (< 80% on a 502s video), so the timeline guard
    fires. `tail_silent` stands in for the ffmpeg tail probe: True = narrator
    fell quiet, False/None = the tail still carries sound. The pre-filter
    returns zero POIs so the run reaches the report stage without screenshots.
    """
    runner = CliRunner()

    video_path = tmp_path / "long.mov"
    video_path.write_bytes(b"video")

    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "long_review"

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 502.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _compressed_timeline_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.tail_is_silent",
        lambda *args, **kwargs: tail_silent,
    )
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: SemanticFilterResult(pois=[], response_id="resp_filter_empty"),
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    return runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            "-o",
            str(output_dir),
            "--no-serve",
            "--skip-validation",
        ],
    )


def test_review_warns_but_continues_when_tail_has_sound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Low coverage + audible tail (possible STT drift) must warn, not abort."""
    result = _run_low_coverage_review(monkeypatch, tmp_path, tail_silent=False)
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Transcript Timeline Warning" in normalized_output
    assert "Transcript timeline coverage is low" in normalized_output
    assert (tmp_path / "long_review" / "long_report.html").exists()


def test_review_continues_quietly_when_tail_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Low coverage from a silent tail (narrator stopped) must not raise a red flag."""
    result = _run_low_coverage_review(monkeypatch, tmp_path, tail_silent=True)
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "narration ended" in normalized_output
    assert "Transcript Timeline Warning" not in normalized_output
    assert (tmp_path / "long_review" / "long_report.html").exists()


def test_review_never_aborts_on_low_coverage_even_when_tail_unmeasurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the tail can't be probed (None), fall back to a warning and keep going."""
    result = _run_low_coverage_review(monkeypatch, tmp_path, tail_silent=None)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "long_review" / "long_report.html").exists()


# --- A2: vision requested but no vision key must warn, not silently skip ------
#
# When vision=True but no vision API key is configured, Step 5 (unified VLM)
# cannot run. Degrading to a silent transcript-only report hides that the
# requested visual pass never happened. The pipeline must warn loudly (yellow
# panel) AND record it in the report errors -- while a genuine --no-vision
# opt-out stays quiet.


def _poi_transcription() -> TranscriptionResult:
    """One actionable segment so the pre-filter can yield a real POI."""
    return TranscriptionResult(
        text="The save button does nothing when I click it.",
        segments=[
            Segment(
                id=0,
                start=0.0,
                end=2.0,
                text="The save button does nothing when I click it.",
                no_speech_prob=0.05,
            ),
        ],
        language="en",
        response_id="resp_stt_poi",
    )


def _one_poi_result() -> SemanticFilterResult:
    """A successful pre-filter with a single point of interest (reaches Step 5)."""
    return SemanticFilterResult(
        pois=[
            PointOfInterest(
                timestamp_start=0.0,
                timestamp_end=2.0,
                category="bug",
                confidence=0.9,
                reasoning="save button does nothing",
                transcript_excerpt="save button",
                segment_ids=[0],
            )
        ],
        response_id="resp_filter_poi",
    )


def _run_review_with_one_poi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: ScreenScribeConfig,
    extra_args: tuple[str, ...] = (),
) -> tuple[object, Path]:
    """Drive `review` to Step 5 with exactly one POI (screenshots stubbed empty).

    Mirrors the empty-state harness but the pre-filter succeeds with a POI, so
    the pipeline reaches the unified-analysis branch. ``extract_screenshots`` is
    patched on ``review_pipeline`` (it is imported there directly, not via cli).
    """
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _poi_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: _one_poi_result(),
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: config))

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            "-o",
            str(output_dir),
            "--no-serve",
            "--skip-validation",
            *extra_args,
        ],
    )
    return result, output_dir


def test_review_warns_when_vision_requested_but_no_vision_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """vision=True + no vision key → loud 'Vision Skipped' + error in the report."""
    config = ScreenScribeConfig(llm_api_key="test-key")  # pragma: allowlist secret
    assert config.get_vision_api_key() == ""  # precondition: vision unavailable

    result, output_dir = _run_review_with_one_poi(monkeypatch, tmp_path, config=config)
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Vision Skipped" in normalized_output
    # Report is still produced (transcript + detections), just transcript-only.
    assert (output_dir / "demo_report.html").exists()
    # The warning must be surfaced in the report, not only on the console.
    report = json.loads((output_dir / "demo_report.json").read_text(encoding="utf-8"))
    error_messages = " ".join(e["message"] for e in report["errors"]).lower()
    assert "vision" in error_messages


def test_review_silent_when_vision_opted_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--no-vision` is a deliberate opt-out → no warning, report still produced."""
    config = ScreenScribeConfig(llm_api_key="test-key")  # pragma: allowlist secret

    result, output_dir = _run_review_with_one_poi(
        monkeypatch, tmp_path, config=config, extra_args=("--no-vision",)
    )
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Vision Skipped" not in normalized_output
    assert (output_dir / "demo_report.html").exists()


# --- A2 follow-up: vision-skip-no-key must not write a lying checkpoint -------
#
# When vision is skipped for a missing key, the VLM/unified stage never ran. The
# pipeline must NOT mark unified_analysis/vision complete and must NOT delete the
# checkpoint on success -- otherwise a later `--resume` (after the key is added)
# trusts a stale "completed" and skips vision, or re-transcribes from scratch.
# The checkpoint must survive and tell the truth.


def test_review_vision_no_key_keeps_truthful_resumable_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """vision=True + no key: report is produced, but the checkpoint must not lie."""
    config = ScreenScribeConfig(llm_api_key="test-key")  # pragma: allowlist secret
    assert config.get_vision_api_key() == ""  # precondition: vision unavailable

    result, output_dir = _run_review_with_one_poi(monkeypatch, tmp_path, config=config)

    assert result.exit_code == 0, result.output
    # Honest partial deliverable now: report + surfaced warning.
    assert (output_dir / "demo_report.html").exists()
    report = json.loads((output_dir / "demo_report.json").read_text(encoding="utf-8"))
    error_messages = " ".join(e["message"] for e in report["errors"]).lower()
    assert "vision" in error_messages

    # Checkpoint must survive and reflect what actually ran.
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None, "checkpoint must be kept so --resume can finish vision"
    assert checkpoint.is_stage_complete("detection")  # semantic prefilter really ran
    assert not checkpoint.is_stage_complete("vision")  # never ran -> not complete
    assert not checkpoint.is_stage_complete("unified_analysis")  # resume gate -> must re-run


def test_review_vision_no_key_checkpoint_resumes_at_unified_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The kept checkpoint routes --resume back to the unified (vision) stage.

    is_stage_complete('unified_analysis') is exactly what gates Step 5 on resume;
    because it is not complete, get_next_stage() returns it -- so a resume with a
    vision key re-runs visual analysis instead of treating it as done.
    """
    config = ScreenScribeConfig(llm_api_key="test-key")  # pragma: allowlist secret

    _, output_dir = _run_review_with_one_poi(monkeypatch, tmp_path, config=config)

    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None
    assert checkpoint.get_next_stage() == "unified_analysis"


def test_review_persists_pruned_screenshots_after_dedup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH42: after unified dedup prunes screenshots/detections, the checkpoint
    must be re-saved with the pruned set. Otherwise a later --resume restores
    the pre-dedup full set and the report regrows the merged duplicates.

    The checkpoint is deleted on success, so we capture every save_checkpoint
    snapshot and assert the unified-stage save holds the pruned screenshots."""
    from screenscribe.unified_analysis import UnifiedFinding

    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"

    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )

    # Two detections -> two screenshots; the unified stage returns two findings
    # that dedup down to one (the second merged into the first).
    def two_shots(video: object, detections: list[object], _dir: object) -> list[object]:
        shots = []
        for i, d in enumerate(detections):
            p = tmp_path / f"shot-{i}.jpg"
            p.write_bytes(b"img")
            shots.append((d, p))
        # Synthesize a second detection/screenshot if the prefilter gave one POI.
        if len(shots) == 1:
            d0 = shots[0][0]
            second = type(d0)(
                segment=type(d0.segment)(id=99, start=5.0, end=6.0, text="dup", no_speech_prob=0.0),
                category=d0.category,
                keywords_found=d0.keywords_found,
                context=d0.context,
            )
            p = tmp_path / "shot-1.jpg"
            p.write_bytes(b"img")
            shots.append((second, p))
        return shots

    def _finding(detection_id: int, ts: float, merged: list[tuple[int, float]]) -> UnifiedFinding:
        return UnifiedFinding(
            detection_id=detection_id,
            screenshot_path=None,
            timestamp=ts,
            category="bug",
            is_issue=True,
            sentiment="problem",
            severity="high",
            summary="finding",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp",
            merged_from_ids=merged,
        )

    def fake_unified(screenshots: list[object], *args: object, **kwargs: object) -> list[object]:
        # One per screenshot.
        return [_finding(d.segment.id, d.segment.start, []) for (d, _p) in screenshots]

    def fake_dedup(findings: list[object]) -> list[object]:
        # Merge the second finding into the first; keep only the first, recording
        # the merged id so the prune keeps both screenshots' keys... we want a
        # real prune, so DROP the second key entirely (not merged) -> 2 -> 1.
        return [findings[0]]

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _poi_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: _one_poi_result(),
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections", two_shots
    )
    monkeypatch.setattr("screenscribe.review_pipeline.analyze_all_findings_unified", fake_unified)
    monkeypatch.setattr("screenscribe.review_pipeline.deduplicate_findings", fake_dedup)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_unified_summary",
        lambda *a, **kw: "summary",
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_visual_summary_unified",
        lambda *a, **kw: "visual",
    )
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: config))

    # Capture every checkpoint save snapshot (screenshot count).
    saved_counts: list[int] = []
    import screenscribe.review_pipeline as rp

    real_save = rp.save_checkpoint

    def capturing_save(checkpoint: object, out: object) -> None:
        saved_counts.append(len(checkpoint.screenshots))
        real_save(checkpoint, out)

    monkeypatch.setattr("screenscribe.review_pipeline.save_checkpoint", capturing_save)

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )

    assert result.exit_code == 0, result.output
    # The Step-4 save recorded 2 screenshots; the unified-stage save (last one)
    # must record the pruned set of 1 (BH42). Pre-fix it stayed at 2.
    assert saved_counts, "expected at least one checkpoint save"
    assert 2 in saved_counts, "screenshot stage should have saved 2 screenshots"
    assert saved_counts[-1] == 1, "unified-stage save must persist the pruned screenshot set"


def test_review_unified_hard_failure_keeps_resumable_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P2-1 + SYS-3: a hard failure in the unified VLM stage must NOT mark
    unified_analysis complete. The stage previously marked itself done even
    after the except caught the crash, so --resume skipped a failed stage as
    done. The checkpoint must survive with unified_analysis unmarked."""
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"img")

    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )

    def boom(*_: object, **__: object) -> object:
        raise RuntimeError("VLM provider exploded")

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _poi_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: _one_poi_result(),
    )
    # Non-empty screenshots so Step 5 reaches the unified call, which then raises.
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda video, detections, _dir: [(detections[0], shot)],
    )
    monkeypatch.setattr("screenscribe.review_pipeline.analyze_all_findings_unified", boom)
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: config))

    result = runner.invoke(
        cli_module.app,
        ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"],
    )

    assert result.exit_code == 0, result.output
    # Report still produced as a partial deliverable.
    assert (output_dir / "demo_report.html").exists()
    report = json.loads((output_dir / "demo_report.json").read_text(encoding="utf-8"))
    stages = " ".join(e["stage"] for e in report["errors"])
    assert "unified_analysis" in stages

    # Checkpoint survives and routes --resume back to the unified stage.
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None, "checkpoint must survive a unified hard failure"
    assert checkpoint.is_stage_complete("screenshots")
    assert not checkpoint.is_stage_complete("unified_analysis")
    assert checkpoint.get_next_stage() == "unified_analysis"


def test_review_records_error_when_all_screenshots_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH50: detections exist but every screenshot failed to extract. With a
    vision key configured, Step 5 would call unified analysis with [] and the
    partial-fail branch (requested_count > 0) never fires, yielding a silent
    transcript-only report. The pipeline must instead surface an explicit error."""
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    assert config.get_vision_api_key() != ""  # precondition: vision IS available

    # The one-POI harness already stubs extract_screenshots to return [],
    # so this is the all-screenshots-lost case once vision is available.
    result, output_dir = _run_review_with_one_poi(monkeypatch, tmp_path, config=config)

    assert result.exit_code == 0, result.output
    assert (output_dir / "demo_report.json").exists()
    report = json.loads((output_dir / "demo_report.json").read_text(encoding="utf-8"))
    stages = " ".join(e["stage"] for e in report["errors"])
    messages = " ".join(e["message"] for e in report["errors"]).lower()
    assert "screenshots" in stages
    assert "no frames" in messages or "no screenshots" in messages or "extract" in messages
