"""Small CLI contract checks for user-facing help text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from screenscribe.cli import app
from screenscribe.config import ScreenScribeConfig
from screenscribe.transcribe import TranscriptionResult
from screenscribe.validation import APIKeyError, validate_models

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """Strip ANSI color codes, then collapse whitespace.

    CI colorizes Rich/Typer output (terminal detection), embedding escape codes
    mid-phrase that a plain ``.split()`` left in place — breaking substring
    asserts that pass locally (no color). Assert on the text contract, not the
    color-rendering substrate.
    """
    # Rich draws box borders (│) into the wrapped help column; drop them before
    # collapsing so help-text asserts see contiguous prose, not border-split words.
    return " ".join(_ANSI_RE.sub("", output).replace("│", " ").split())


def test_analyze_help_describes_runtime_language_contract() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["analyze", "--help"])
    normalized_output = _plain(result.output)

    assert result.exit_code == 0
    assert "default dashboard language" in normalized_output
    assert "PL/EN toggle controls the UI and future VLM analyses" in normalized_output
    assert "always English regardless of --lang" not in normalized_output


def test_review_help_documents_no_vision_not_no_ai() -> None:
    """The visual-analysis flag is honest: --no-vision skips VLM, LLM still runs."""
    runner = CliRunner()

    # Force a wide terminal so Rich does not hard-wrap the locked help sentence.
    result = runner.invoke(app, ["review", "--help"], env={"COLUMNS": "200"})
    normalized_output = _plain(result.output)

    assert result.exit_code == 0
    assert "--no-vision" in normalized_output
    assert "Skip visual/screenshot analysis. Semantic LLM detection still runs." in (
        normalized_output
    )
    # The dishonest legacy name is gone from the user-facing surface.
    assert "--no-ai" not in normalized_output


def test_review_no_vision_disables_vlm_but_keeps_screenshots(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """--no-vision sets vision=False into the pipeline; screenshot extraction is unaffected.

    Refinement 1 of the locked contract: --no-vision skips VLM image reasoning, NOT
    screenshot extraction. We assert the flag flows through as vision=False; the
    pipeline (review_pipeline) extracts screenshots unconditionally and only gates the
    unified VLM call on this boolean.
    """
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(stt_api_key="test-key"),
    )
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli._require_audio_or_exit", lambda _video: None)

    def fake_run_review(*_args: Any, **kwargs: Any) -> None:
        recorded["vision"] = kwargs["vision"]

    monkeypatch.setattr("screenscribe.review_pipeline.run_review", fake_run_review)

    for flag in ("--no-vision", "--no-vlm"):
        recorded.clear()
        result = runner_invoke_review(video, flag)
        assert result.exit_code == 0, result.output
        assert recorded["vision"] is False


def runner_invoke_review(video: Path, flag: str) -> Any:
    """Invoke the review command with a single extra flag, skipping validation."""
    return CliRunner().invoke(
        app,
        ["review", str(video), flag, "--no-serve", "--skip-validation"],
    )


def test_transcribe_uses_config_language_unless_lang_overrides(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A configured PL transcript must not silently fall back to CLI default EN."""
    video = tmp_path / "sample.mov"
    audio = tmp_path / "sample.mp3"
    video.write_bytes(b"fake-video")
    audio.write_bytes(b"fake-audio")
    recorded_languages: list[str] = []

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="pl", stt_api_key="test-key"),
    )
    monkeypatch.setattr("screenscribe.cli._extract_audio_or_exit", lambda _video: audio)

    def fake_transcribe_audio(
        *_args: object, language: str, **_kwargs: object
    ) -> TranscriptionResult:
        recorded_languages.append(language)
        return TranscriptionResult(text=f"language={language}", segments=[], language=language)

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio", fake_transcribe_audio)

    runner = CliRunner()

    result = runner.invoke(app, ["transcribe", str(video)])
    assert result.exit_code == 0

    result = runner.invoke(app, ["transcribe", str(video), "--lang", "en"])
    assert result.exit_code == 0

    assert recorded_languages == ["pl", "en"]


def test_transcribe_auth_error_is_friendly(monkeypatch: Any, tmp_path: Path) -> None:
    video = tmp_path / "sample.mov"
    audio = tmp_path / "sample.mp3"
    video.write_bytes(b"fake-video")
    audio.write_bytes(b"fake-audio")

    def raise_401(*_args: object, **_kwargs: object) -> TranscriptionResult:
        request = httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")
        response = httpx.Response(
            401,
            json={"message": "Invalid API key"},
            request=request,
        )
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(api_key="bad-key"),
    )
    monkeypatch.setattr("screenscribe.cli._extract_audio_or_exit", lambda _video: audio)
    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio", raise_401)

    result = CliRunner().invoke(app, ["transcribe", str(video)])
    normalized_output = _plain(result.output)

    assert result.exit_code == 1, result.output
    assert "Transcription Failed" in normalized_output
    assert "rejected the credentials" in normalized_output
    assert "SCREENSCRIBE_API_KEY" in normalized_output
    assert "screenscribe config setup" in normalized_output
    assert "--set-key" not in normalized_output
    assert "Traceback" not in result.output
    assert "HTTPStatusError" not in result.output


def test_no_key_guidance_prefers_safe_provider_setup() -> None:
    with pytest.raises(APIKeyError) as exc_info:
        validate_models(ScreenScribeConfig())

    assert str(exc_info.value).startswith("No API key configured. Run `screenscribe config setup`")


def test_runtime_guidance_never_recommends_secret_in_command_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime_sources = (
        root / "screenscribe" / "transcribe.py",
        root / "screenscribe" / "cli_messages.py",
    )

    for source in runtime_sources:
        text = source.read_text(encoding="utf-8")
        assert "config setup" in text
        assert "config --set-key" not in text


def test_review_estimate_does_not_require_api_key(monkeypatch: Any, tmp_path: Path) -> None:
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli._require_audio_or_exit", lambda _video: None)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _video: 120.0)
    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(),
    )

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("estimate should not validate API models")

    monkeypatch.setattr("screenscribe.cli.validate_models", fail_if_called)

    result = CliRunner().invoke(app, ["review", str(video), "--estimate"])
    normalized_output = _plain(result.output)

    assert result.exit_code == 0, result.output
    assert "Estimated Processing Time" in normalized_output
    assert "Total estimated time" in normalized_output
    assert "API Key Error" not in normalized_output
    assert "estimate should not validate API models" not in normalized_output


def test_analyze_uses_config_language_unless_lang_overrides(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The analyze dashboard must honor the configured language too — the
    review/transcribe/preprocess fix left analyze clobbering it with EN."""
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")
    recorded_languages: list[str] = []

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="pl", vision_api_key="test-key"),
    )

    class _FakeApp:
        class state:
            session_token = "t"  # noqa: S105 - test fixture, not a credential

    def fake_create_analyze_app(_video: Path, config: ScreenScribeConfig) -> _FakeApp:
        recorded_languages.append(config.language)
        return _FakeApp()

    monkeypatch.setattr("screenscribe.analyze_server.create_analyze_app", fake_create_analyze_app)
    monkeypatch.setattr(
        "screenscribe.cli.webbrowser", type("W", (), {"open": staticmethod(lambda _u: None)})
    )
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: None)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *_a, **_k: None)

    runner = CliRunner()

    result = runner.invoke(app, ["analyze", str(video)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["analyze", str(video), "--lang", "en"])
    assert result.exit_code == 0, result.output

    assert recorded_languages == ["pl", "en"]


def test_analyze_keywords_file_reaches_server_config(monkeypatch: Any, tmp_path: Path) -> None:
    """`analyze --keywords-file` must load the dictionary into the config that
    backs the live server's session — not only the CLI review path (refinement 3).

    We capture the config handed to ``create_analyze_app`` and assert the explicit
    per-run keywords landed on ``config.keywords``.
    """
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")

    keywords_path = tmp_path / "team-keywords.yaml"
    keywords_path.write_text('bug:\n  - "klikam i nic"\nui:\n  - "potworek"\n', encoding="utf-8")

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en", vision_api_key="test-key"),
    )

    recorded: list[ScreenScribeConfig] = []

    class _FakeApp:
        class state:
            session_token = "t"  # noqa: S105 - test fixture, not a credential

    def fake_create_analyze_app(_video: Path, config: ScreenScribeConfig) -> _FakeApp:
        recorded.append(config)
        return _FakeApp()

    monkeypatch.setattr("screenscribe.analyze_server.create_analyze_app", fake_create_analyze_app)
    monkeypatch.setattr(
        "screenscribe.cli.webbrowser", type("W", (), {"open": staticmethod(lambda _u: None)})
    )
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: None)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *_a, **_k: None)

    result = CliRunner().invoke(app, ["analyze", str(video), "--keywords-file", str(keywords_path)])
    assert result.exit_code == 0, result.output

    assert recorded, "create_analyze_app should have been called"
    config = recorded[0]
    assert config.keywords is not None
    assert config.keywords.bug == ["klikam i nic"]
    assert config.keywords.ui == ["potworek"]


def _stub_analyze_server_side_effects(monkeypatch: Any, recorded: list[ScreenScribeConfig]) -> None:
    """Stub the live-server machinery so `analyze` can be driven headless."""

    class _FakeApp:
        class state:
            session_token = "t"  # noqa: S105 - test fixture, not a credential

    def fake_create_analyze_app(_video: Path, config: ScreenScribeConfig) -> _FakeApp:
        recorded.append(config)
        return _FakeApp()

    monkeypatch.setattr("screenscribe.analyze_server.create_analyze_app", fake_create_analyze_app)
    monkeypatch.setattr(
        "screenscribe.cli.webbrowser", type("W", (), {"open": staticmethod(lambda _u: None)})
    )
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: None)
    # analyze now runs a model-availability pre-flight; stub it out so the headless
    # drive never touches the network.
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *_a, **_k: None)


def test_analyze_blocks_known_key_endpoint_mismatch(monkeypatch: Any, tmp_path: Path) -> None:
    """A known OpenAI-to-Libraxis mismatch blocks before the analyze runtime."""
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(
            language="en",
            vision_api_key="sk-openai-secret",  # pragma: allowlist secret
        ),  # endpoints stay at LibraxisAI defaults -> mismatch
    )

    recorded: list[ScreenScribeConfig] = []
    _stub_analyze_server_side_effects(monkeypatch, recorded)

    result = CliRunner().invoke(app, ["analyze", str(video)])

    assert result.exit_code == 1, result.output
    assert not recorded
    out = _plain(result.output)
    assert "Config Warning:" in out
    assert "Config Error:" in out
    assert "mismatch" in out.lower()
    assert "sk-openai-secret" not in result.output  # secret never echoed


def test_analyze_passes_on_clean_config(monkeypatch: Any, tmp_path: Path) -> None:
    """C5.5 A4: a matching key<->endpoint config is not blocked by the new check."""
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en", vision_api_key="test-key"),
    )

    recorded: list[ScreenScribeConfig] = []
    _stub_analyze_server_side_effects(monkeypatch, recorded)

    result = CliRunner().invoke(app, ["analyze", str(video)])

    assert result.exit_code == 0, result.output
    assert recorded, "create_analyze_app should have run on a clean config"


def test_analyze_does_not_block_on_unused_llm_mismatch(monkeypatch: Any, tmp_path: Path) -> None:
    """Finding I: `analyze` uses vision (frame analysis) + STT (voice notes), never
    the LLM. A stale/invalid LLM key<->endpoint mismatch must NOT block analyze --
    that endpoint is never contacted during frame analysis. Pre-fix the full
    ``config.validate()`` flags the unused-LLM mismatch and exits 1."""
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(
            language="en",
            vision_api_key="test-vision-key",  # pragma: allowlist secret  (clean vision)
            llm_api_key="sk-stale-openai",  # pragma: allowlist secret  (openai key on libraxis LLM endpoint)
        ),
    )

    recorded: list[ScreenScribeConfig] = []
    _stub_analyze_server_side_effects(monkeypatch, recorded)

    result = CliRunner().invoke(app, ["analyze", str(video)])

    assert result.exit_code == 0, result.output
    assert recorded, "analyze must start despite an unused LLM key/endpoint mismatch"


def test_analyze_still_requires_api_key(monkeypatch: Any, tmp_path: Path) -> None:
    """C5.5 A5: the existing presence-only API-key check is preserved."""
    video = tmp_path / "sample.mov"
    video.write_bytes(b"fake-video")

    monkeypatch.setattr(
        "screenscribe.cli.ScreenScribeConfig.load",
        lambda: ScreenScribeConfig(language="en"),  # no key at all
    )

    recorded: list[ScreenScribeConfig] = []
    _stub_analyze_server_side_effects(monkeypatch, recorded)

    result = CliRunner().invoke(app, ["analyze", str(video)])

    assert result.exit_code == 1, result.output
    assert not recorded
    assert "API key required" in _plain(result.output)


def test_config_show_fully_redacts_api_key(monkeypatch: Any) -> None:
    """`config --show` must never leak any character of a real API key.

    The old renderer printed the last 8 characters of each key. This asserts
    full redaction: not even the last-4 suffix of any configured key may
    appear in the output.
    """
    # Distinct, synthetic fixtures per slot so a leak from any one is caught.
    # The `sk-` prefix is split from the body so the literal token never appears
    # contiguously in source — otherwise the repo leak-scan (`sk-[A-Za-z0-9_-]{20,}`,
    # a grep with no per-line allowlist) would red the release gate on this very test.
    _sk = "sk-"
    cfg = ScreenScribeConfig(
        api_key=_sk + "MAINaaaaaaaaaaaaaaaaXY12main",
        stt_api_key=_sk + "STTbbbbbbbbbbbbbbbbZZ34sttx",
        llm_api_key=_sk + "LLMcccccccccccccccQQ56llmx",
        vision_api_key=_sk + "VISddddddddddddddWW78visn",
        stt_fallback_api_key=_sk + "FBKeeeeeeeeeeeeeeVV90fbck",
    )
    monkeypatch.setattr(
        "screenscribe.config.ScreenScribeConfig.load", classmethod(lambda _cls: cfg)
    )

    result = CliRunner().invoke(app, ["config", "--show"])
    assert result.exit_code == 0, result.output

    # No suffix (last 4 chars) of any key may survive into the output.
    for key in (
        cfg.api_key,
        cfg.stt_api_key,
        cfg.llm_api_key,
        cfg.vision_api_key,
        cfg.stt_fallback_api_key,
    ):
        assert key[-4:] not in result.output, f"leaked suffix of {key!r}: {result.output!r}"
        assert key not in result.output

    # The masked placeholder is present (keys are shown, just redacted).
    assert "Main:" in result.output
    assert "*" in result.output
