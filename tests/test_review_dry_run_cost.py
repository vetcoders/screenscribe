"""C6.3: --dry-run cost disclosure.

`review --dry-run` is NOT zero-cost: it still runs Step 2 transcription (paid STT
unless --local) and Step 3 issue detection (the LLM semantic prefilter, ALWAYS
paid) before exiting. The flag name is NOT changed (renaming --dry-run is a
public CLI / product decision); instead a pre-flight cost warning is emitted so
the user can abort, and the help text tells the truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli_module
from screenscribe.config import ScreenScribeConfig
from screenscribe.semantic_filter import SemanticFilterResult
from screenscribe.transcribe import Segment, TranscriptionResult


def _transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="No actionable issues here.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="No actionable issues.", no_speech_prob=0.1)
        ],
        language="en",
        response_id="resp_stt_dry",
    )


def _run_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: ScreenScribeConfig,
    extra_args: tuple[str, ...] = (),
) -> object:
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
        "screenscribe.transcribe.transcribe_audio", lambda *a, **kw: _transcription()
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(pois=[], response_id="resp_filter_dry"),
    )
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: config))

    return runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            "-o",
            str(output_dir),
            "--no-serve",
            "--skip-validation",
            "--dry-run",
            *extra_args,
        ],
    )


def test_dry_run_paid_path_warns_about_stt_and_llm_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A2: without --local, the warning names paid STT + LLM detection."""
    config = ScreenScribeConfig(api_key="test-key")  # pragma: allowlist secret
    result = _run_dry_run(monkeypatch, tmp_path, config=config)
    out = " ".join(result.output.split()).lower()

    assert result.exit_code == 0, result.output
    assert "dry-run is not free" in out
    assert "cost" in out  # title "Dry-run incurs API cost"
    assert "transcription (stt)" in out or "stt" in out
    assert "llm" in out
    # Points the user at the real zero-cost path.
    assert "--estimate" in result.output
    # Dry-run still completes and writes no report.
    assert not (tmp_path / "demo_review" / "demo_report.html").exists()


def test_dry_run_local_path_warns_about_llm_but_not_paid_stt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A3: with --local the warning must NOT claim paid STT, but MUST still warn
    that LLM issue detection costs money."""
    config = ScreenScribeConfig(api_key="test-key")  # pragma: allowlist secret
    result = _run_dry_run(monkeypatch, tmp_path, config=config, extra_args=("--local",))
    out = " ".join(result.output.split()).lower()

    assert result.exit_code == 0, result.output
    assert "dry-run is not free" in out
    # LLM detection cost is still disclosed.
    assert "llm" in out and ("api cost" in out or "incurs" in out)
    # It must NOT falsely claim STT is paid under --local.
    assert "no stt cost" in out or "no stt" in out


def test_dry_run_help_text_discloses_cost() -> None:
    """A1: `review --help` exposes the paid nature of --dry-run."""
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["review", "--help"])
    text = " ".join(result.output.split()).lower()

    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output
    # Help no longer sells dry-run as a free preview; it names the paid path and
    # the real zero-cost alternative.
    assert "paid" in text
    assert "--estimate" in result.output


def test_dry_run_flag_not_renamed() -> None:
    """A5 (STOP respected): the public flag stays --dry-run; no --preview added."""
    cli_source = Path(cli_module.__file__).read_text(encoding="utf-8")
    assert '"--dry-run"' in cli_source
    assert '"--preview"' not in cli_source
