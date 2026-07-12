from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli
from screenscribe.cli import app
from screenscribe.config import (
    LIBRAXIS_API_BASE,
    OPENAI_API_BASE,
    OPENAI_LLM_MODEL,
    OPENAI_STT_MODEL,
    ScreenScribeConfig,
)

runner = CliRunner()


def _config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path / ".config" / "screenscribe" / "config.env"


def test_libraxis_preset_is_coherent() -> None:
    config = ScreenScribeConfig.provider_preset("libraxis", "lx-test-key")

    assert config.provider == "libraxis"
    assert config.api_base == LIBRAXIS_API_BASE
    assert config.llm_model == "programmer"
    assert config.validate() == []


def test_openai_preset_sets_all_endpoints_and_compatible_models() -> None:
    config = ScreenScribeConfig.provider_preset(
        "openai",
        "sk-" + "openai-test-key",  # pragma: allowlist secret
    )

    assert config.provider == "openai"
    assert config.api_base == OPENAI_API_BASE
    assert config.stt_endpoint == f"{OPENAI_API_BASE}/v1/audio/transcriptions"
    assert config.llm_endpoint == f"{OPENAI_API_BASE}/v1/responses"
    assert config.vision_endpoint == f"{OPENAI_API_BASE}/v1/responses"
    assert config.stt_model == OPENAI_STT_MODEL
    assert config.llm_model == OPENAI_LLM_MODEL
    assert config.vision_model == OPENAI_LLM_MODEL
    assert config.validate() == []


def test_custom_preset_preserves_user_endpoint_and_models() -> None:
    config = ScreenScribeConfig.provider_preset(
        "custom",
        "custom-key",  # pragma: allowlist secret
        custom_base="https://provider.example/api",
        stt_model="custom-stt",
        llm_model="custom-llm",
        vision_model="custom-vision",
    )

    assert config.provider == "custom"
    assert config.stt_endpoint == "https://provider.example/api/v1/audio/transcriptions"
    assert config.llm_model == "custom-llm"
    assert config.configuration_status().startswith("READY WITH WARNING")


def test_setup_uses_hidden_prompt_and_saves_complete_openai_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config_path(monkeypatch, tmp_path)
    secret = "sk-" + "hidden-openai-key"  # pragma: allowlist secret

    result = runner.invoke(app, ["config", "setup"], input=f"2\n{secret}\n")

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    text = path.read_text()
    assert "SCREENSCRIBE_PROVIDER=openai" in text
    assert f"SCREENSCRIBE_API_KEY={secret}" in text  # pragma: allowlist secret
    assert f"SCREENSCRIBE_API_BASE={OPENAI_API_BASE}" in text
    assert f"SCREENSCRIBE_LLM_MODEL={OPENAI_LLM_MODEL}" in text


def test_custom_setup_is_marked_advanced_and_explains_every_technical_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        ["config", "setup"],
        input=(
            "3\ncustom-key\nhttps://api.example.com\nwhisper-1\nprovider-text\nprovider-vision\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Custom OpenAI-compatible provider (advanced)" in result.output
    assert "API base URL (for example: https://api.example.com)" in result.output
    assert "STT model (for example: whisper-1)" in result.output
    assert "LLM model (for example: your-provider-text-model)" in result.output
    assert "Vision model (for example: your-provider-vision-model)" in result.output


def test_atomic_setup_write_preserves_existing_config_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config_path(monkeypatch, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("ORIGINAL=1\n")
    config = ScreenScribeConfig.provider_preset("libraxis", "replacement-key")
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(
        os,
        "close",
        lambda *_args: pytest.fail("wrapped file descriptor must not be closed twice"),
    )

    with pytest.raises(OSError, match="boom"):
        config.save_default_config()

    assert path.read_text() == "ORIGINAL=1\n"
    assert list(path.parent.glob(".config.env.*")) == []


def test_known_mismatch_blocks_before_any_runtime_or_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "sample.mov"
    video.write_bytes(b"video")
    config = ScreenScribeConfig(
        api_key="sk-" + "openai-key",  # pragma: allowlist secret
        provider="libraxis",
    )
    monkeypatch.setattr(cli.ScreenScribeConfig, "load", classmethod(lambda _cls: config))
    reached_runtime = False

    def fail_if_reached() -> None:
        nonlocal reached_runtime
        reached_runtime = True

    monkeypatch.setattr(cli, "_check_ffmpeg_or_exit", fail_if_reached)
    result = runner.invoke(app, ["transcribe", str(video)])

    assert result.exit_code == 1
    assert reached_runtime is False
    assert "No request was sent" in result.output


def test_config_show_reports_provider_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ScreenScribeConfig.provider_preset("libraxis", "configured-key")
    monkeypatch.setattr(cli.ScreenScribeConfig, "load", classmethod(lambda _cls: config))

    result = runner.invoke(app, ["config", "--show"])

    assert result.exit_code == 0, result.output
    assert "LibraxisAI" in result.output
    assert "READY" in result.output


def test_legacy_set_key_warns_about_shell_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _cls: ScreenScribeConfig())
    )

    result = runner.invoke(app, ["config", "--set-key", "legacy-key"])

    assert result.exit_code == 0
    assert "shell history" in result.output
    assert "config setup" in result.output
