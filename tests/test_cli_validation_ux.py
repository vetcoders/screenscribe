"""CLI validation + UX honesty tests (cut W2-A2).

Each test pins one finding from the quality recon:

- --local still validates the cloud LLM/Vision (only STT is skipped).
- analyze runs a model-availability pre-flight instead of failing mid-pipeline.
- transcribe -o creates missing output parent dirs (no raw traceback).
- an sk- key on a non-OpenAI endpoint is a WARNING, not a hard block.
- --embed-video warns when a clip is too large to embed (>=50MB).
- --estimate on an audioless clip exits 0 with the estimate (no audio guard).

Only true externals are mocked (ffmpeg, audio, duration, validation, pipeline).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

import screenscribe.cli as cli
from screenscribe.cli import app
from screenscribe.config import ScreenScribeConfig
from screenscribe.validation import ModelValidationError

runner = CliRunner()


def _mkvideo(tmp_path: Path, name: str = "demo.mov") -> Path:
    video = tmp_path / name
    video.write_bytes(b"fake-video")
    return video


# --------------------------------------------------------------------------- #
# Finding 1: --local still validates the cloud LLM/Vision (STT is local)
# --------------------------------------------------------------------------- #


def test_review_local_still_validates_cloud_models(tmp_path: Path, monkeypatch: Any) -> None:
    """--local only reroutes STT to a local Whisper server; the LLM pre-filter and
    the Vision stage still hit the cloud and MUST be validated. The old gate
    disabled ALL validation under --local -- a cloud LLM outage then failed
    silently mid-pipeline instead of at pre-flight."""
    video = _mkvideo(tmp_path)

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    captured: dict[str, Any] = {}

    def record_then_fail(_config: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        raise ModelValidationError(
            "Cannot connect to API", model_type="LLM", model_name="programmer"
        )

    monkeypatch.setattr(cli, "validate_models", record_then_fail)

    result = runner.invoke(app, ["review", str(video), "--local", "--no-serve"])

    # Validation ran under --local and surfaced the cloud LLM failure at pre-flight.
    assert result.exit_code == 1, result.output
    assert "Model Error" in result.output
    assert "LLM" in result.output
    # STT probe skipped (local Whisper), LLM/Vision still validated.
    assert captured.get("validate_stt") is False
    assert captured.get("use_vision") is True


# --------------------------------------------------------------------------- #
# Finding 2: analyze runs a model-availability pre-flight
# --------------------------------------------------------------------------- #


def _stub_analyze_server(monkeypatch: Any, recorded: list[ScreenScribeConfig]) -> None:
    class _FakeApp:
        class state:
            session_token = "t"  # noqa: S105 - test fixture, not a credential

    def fake_create(_video: Path, config: ScreenScribeConfig) -> _FakeApp:
        recorded.append(config)
        return _FakeApp()

    monkeypatch.setattr("screenscribe.analyze_server.create_analyze_app", fake_create)
    monkeypatch.setattr(
        "screenscribe.cli.webbrowser", type("W", (), {"open": staticmethod(lambda _u: None)})
    )
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: None)


def test_analyze_preflight_fails_on_dead_model(tmp_path: Path, monkeypatch: Any) -> None:
    """analyze must probe model availability up front (mirror review). A present-
    but-dead key used to pass the presence-only check and fail deep in the first
    frame analysis; now it fails at pre-flight with a clear message and the server
    never starts."""
    video = _mkvideo(tmp_path)

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en", vision_api_key="present-but-dead"),
    )

    recorded: list[ScreenScribeConfig] = []
    _stub_analyze_server(monkeypatch, recorded)

    def raise_model(*_a: Any, **_k: Any) -> None:
        raise ModelValidationError(
            "Vision model 'programmer' not found",
            model_type="Vision",
            model_name="programmer",
        )

    monkeypatch.setattr("screenscribe.cli.validate_models", raise_model)

    result = runner.invoke(app, ["analyze", str(video)])

    assert result.exit_code == 1, result.output
    assert "Model Error" in result.output
    assert not recorded, "create_analyze_app must NOT run when pre-flight fails"


def test_analyze_preflight_skips_llm_probe(tmp_path: Path, monkeypatch: Any) -> None:
    """analyze uses Vision (frame analysis) + STT (voice notes), never the LLM, so
    the pre-flight must NOT probe the LLM endpoint."""
    video = _mkvideo(tmp_path)

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en", vision_api_key="test-key"),
    )

    recorded: list[ScreenScribeConfig] = []
    _stub_analyze_server(monkeypatch, recorded)

    captured: dict[str, Any] = {}

    def record(_config: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("screenscribe.cli.validate_models", record)

    result = runner.invoke(app, ["analyze", str(video)])

    assert result.exit_code == 0, result.output
    assert recorded, "server should start on a clean pre-flight"
    assert captured.get("validate_llm") is False
    assert captured.get("validate_stt") is True
    assert captured.get("use_vision") is True


# --------------------------------------------------------------------------- #
# Finding 3: transcribe -o creates missing parent directories
# --------------------------------------------------------------------------- #


def test_transcribe_creates_missing_output_parent_dirs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`transcribe -o a/b/c.txt` where a/b does not exist must succeed (the parent
    tree is created) instead of crashing with a raw FileNotFoundError traceback."""
    video = _mkvideo(tmp_path)
    out = tmp_path / "does" / "not" / "exist" / "transcript.txt"

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_extract_audio_or_exit", lambda _v: tmp_path / "audio.mp3")
    monkeypatch.setattr(cli, "get_video_duration", lambda _v: 12.0)
    monkeypatch.setattr(
        cli, "_transcribe_audio_or_exit", lambda *a, **k: SimpleNamespace(text="hello world")
    )
    monkeypatch.setattr(cli, "filter_hallucinated_segments", lambda result, *a, **k: result)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    result = runner.invoke(app, ["transcribe", str(video), "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert out.exists()
    assert out.read_text(encoding="utf-8") == "hello world"


# --------------------------------------------------------------------------- #
# Finding 4: sk- key on a non-OpenAI endpoint is a warning, not a block
# --------------------------------------------------------------------------- #


def test_review_warns_but_does_not_block_on_sk_mismatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An sk- key on the default (LibraxisAI) endpoint used to hard-block the run.
    OpenAI-compatible gateways legitimately use sk- keys, so review now warns and
    proceeds."""
    video = _mkvideo(tmp_path)

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(cli, "validate_models", lambda *a, **k: None)
    monkeypatch.setattr("screenscribe.review_pipeline.run_review", lambda *a, **k: None)
    monkeypatch.setattr(
        cli.ScreenScribeConfig,
        "load",
        classmethod(
            lambda _c: ScreenScribeConfig(
                llm_api_key="sk-openai-secret",  # pragma: allowlist secret
            )
        ),
    )

    result = runner.invoke(app, ["review", str(video), "--no-serve"])

    assert result.exit_code == 0, result.output
    assert "Config Warning:" in result.output
    assert "mismatch" in result.output.lower()
    assert "sk-openai-secret" not in result.output  # secret never echoed


# --------------------------------------------------------------------------- #
# Finding 8: --embed-video warns on oversized clips (>=50MB)
# --------------------------------------------------------------------------- #


def test_review_warns_on_oversized_embed_video(tmp_path: Path, monkeypatch: Any) -> None:
    """--embed-video silently degrades to a file reference for clips >=50MB. Warn up
    front so the missing embed is not a surprise. Driven through --estimate so no
    pipeline runs."""
    video = tmp_path / "big.mov"
    # Sparse 50MB+1 file: no real disk write, stat().st_size reports the full size.
    with open(video, "wb") as f:
        f.seek(50 * 1024 * 1024)
        f.write(b"\0")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "get_video_duration", lambda _v: 60.0)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    result = runner.invoke(app, ["review", str(video), "--embed-video", "--estimate"])

    assert result.exit_code == 0, result.output
    assert "big.mov" in result.output
    assert "reference" in result.output.lower()


# --------------------------------------------------------------------------- #
# Finding 9: --estimate on an audioless clip exits 0 (no audio guard)
# --------------------------------------------------------------------------- #


def test_estimate_skips_audio_guard_and_exits_zero(tmp_path: Path, monkeypatch: Any) -> None:
    """--estimate is a zero-cost preview that needs only the container duration, not
    a decoded audio stream. The audio guard must be skipped so an audioless clip
    still gets its estimate table (exit 0) instead of a hard exit."""
    video = _mkvideo(tmp_path)

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "get_video_duration", lambda _v: 90.0)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    def must_not_run(_v: Path) -> None:
        raise AssertionError("_require_audio_or_exit must not run under --estimate")

    monkeypatch.setattr(cli, "_require_audio_or_exit", must_not_run)

    result = runner.invoke(app, ["review", str(video), "--estimate"])

    assert result.exit_code == 0, result.output
    assert "Estimated Processing Time" in result.output
