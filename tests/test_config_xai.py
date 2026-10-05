"""xAI provider preset, TTS / live-STT config fields and env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from screenscribe.config import (
    XAI_API_BASE,
    XAI_LLM_MODEL,
    XAI_STT_ENDPOINT,
    XAI_STT_LIVE_ENDPOINT,
    XAI_TTS_ENDPOINT,
    ScreenScribeConfig,
)

_ENV_KEYS = (
    "SCREENSCRIBE_PROVIDER",
    "SCREENSCRIBE_API_KEY",
    "SCREENSCRIBE_STT_API_KEY",
    "SCREENSCRIBE_TTS_ENDPOINT",
    "SCREENSCRIBE_TTS_API_KEY",
    "SCREENSCRIBE_TTS_VOICE",
    "SCREENSCRIBE_STT_LIVE_ENDPOINT",
    "SCREENSCRIBE_STT_ENDPOINT",
    "SCREENSCRIBE_LLM_ENDPOINT",
    "SCREENSCRIBE_VISION_ENDPOINT",
    "SCREENSCRIBE_API_BASE",
    "LIBRAXIS_API_BASE",
    "OPENAI_API_KEY",
    "LIBRAXIS_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_xai_preset_is_coherent() -> None:
    config = ScreenScribeConfig.provider_preset("xai", "xai-" + "key")

    assert config.provider == "xai"
    assert config.api_base == XAI_API_BASE == "https://api.x.ai"
    assert config.stt_endpoint == XAI_STT_ENDPOINT == "https://api.x.ai/v1/stt"
    assert config.llm_endpoint == "https://api.x.ai/v1/responses"
    assert config.vision_endpoint == "https://api.x.ai/v1/responses"
    assert config.stt_model == ""  # xAI STT takes no model
    assert config.llm_model == XAI_LLM_MODEL == "grok-4.6"
    assert config.tts_endpoint == XAI_TTS_ENDPOINT == "https://api.x.ai/v1/tts"
    assert config.stt_live_endpoint == XAI_STT_LIVE_ENDPOINT == "wss://api.x.ai/v1/stt"
    assert config.validate() == []
    assert config.mismatch_warnings() == []
    assert config.recognized_provider() == "xai"
    assert config.configuration_status() == "READY"


def test_endpoint_provider_tags_x_ai_host() -> None:
    assert ScreenScribeConfig._endpoint_provider("https://api.x.ai/v1/stt") == "xai"
    assert ScreenScribeConfig._endpoint_provider("wss://api.x.ai/v1/stt") == "xai"
    assert ScreenScribeConfig._endpoint_provider("https://x.ai.evil.example/v1") is None


def test_libraxis_key_on_xai_endpoint_is_blocked() -> None:
    config = ScreenScribeConfig.provider_preset("xai", "sk-vista-" + "secret")
    errors = config.validate({"stt"})
    assert errors, "a LibraxisAI key must never be sent to api.x.ai"
    assert "No request was sent" in errors[0]
    assert "secret" not in "\n".join(errors)


def test_xai_endpoints_do_not_trigger_libraxis_path_checks() -> None:
    # /v1/chat/completions is only a hard error on LibraxisAI hosts.
    config = ScreenScribeConfig.provider_preset("xai", "k")
    config.llm_endpoint = "https://api.x.ai/v1/chat/completions"
    config.provider = ""
    assert config.validate() == []


def test_new_fields_default_empty_and_env_overrides_land() -> None:
    config = ScreenScribeConfig()
    assert (config.tts_endpoint, config.tts_api_key, config.tts_voice) == ("", "", "")
    assert config.stt_live_endpoint == ""


def test_env_overrides_follow_screenscribe_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENSCRIBE_TTS_ENDPOINT", "https://api.x.ai/v1/tts/")
    monkeypatch.setenv("SCREENSCRIBE_TTS_API_KEY", "tts-" + "key")
    monkeypatch.setenv("SCREENSCRIBE_TTS_VOICE", "ara")
    monkeypatch.setenv("SCREENSCRIBE_STT_LIVE_ENDPOINT", "wss://api.x.ai/v1/stt/")

    config = ScreenScribeConfig()
    config._load_from_env()

    assert config.tts_endpoint == "https://api.x.ai/v1/tts"
    assert config.tts_api_key == "tts-key"  # pragma: allowlist secret
    assert config.tts_voice == "ara"
    assert config.stt_live_endpoint == "wss://api.x.ai/v1/stt"


def test_config_file_lines_land_in_new_fields(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text(
        "SCREENSCRIBE_API_KEY=main-key\n"
        "SCREENSCRIBE_TTS_API_KEY=tts-key\n"
        "SCREENSCRIBE_TTS_ENDPOINT=https://api.x.ai/v1/tts\n"
        "SCREENSCRIBE_TTS_VOICE=eve\n"
        "SCREENSCRIBE_STT_LIVE_ENDPOINT=wss://api.x.ai/v1/stt\n"
        "SCREENSCRIBE_STT_ENDPOINT=https://api.x.ai/v1/stt\n",
        encoding="utf-8",
    )
    config = ScreenScribeConfig()
    config._load_from_file(path)

    assert config.api_key == "main-key"  # pragma: allowlist secret
    assert config.tts_api_key == "tts-key"  # pragma: allowlist secret
    assert config.tts_endpoint == "https://api.x.ai/v1/tts"
    assert config.tts_voice == "eve"
    assert config.stt_live_endpoint == "wss://api.x.ai/v1/stt"
    assert config.stt_endpoint == "https://api.x.ai/v1/stt"


def test_tts_and_live_helpers_derive_from_stt_provider() -> None:
    xai = ScreenScribeConfig.provider_preset("xai", "k")
    assert xai.get_tts_api_key() == "k"
    assert xai.get_tts_endpoint() == "https://api.x.ai/v1/tts"
    assert xai.get_stt_live_endpoint() == "wss://api.x.ai/v1/stt"

    libraxis = ScreenScribeConfig.provider_preset("libraxis", "lx")
    assert libraxis.get_tts_endpoint() == ""  # no TTS provider wired for LibraxisAI
    assert libraxis.get_stt_live_endpoint() == "wss://api.libraxis.cloud/v1/audio/transcribe"

    explicit = ScreenScribeConfig(tts_api_key="t", tts_endpoint="https://api.x.ai/v1/tts")
    assert explicit.get_tts_api_key() == "t"


def test_save_default_config_round_trips_new_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = ScreenScribeConfig.provider_preset("xai", "xai-" + "key")
    config.tts_voice = "ara"
    path = config.save_default_config()
    text = path.read_text(encoding="utf-8")

    assert "SCREENSCRIBE_PROVIDER=xai" in text
    assert "SCREENSCRIBE_TTS_ENDPOINT=https://api.x.ai/v1/tts" in text
    assert "SCREENSCRIBE_TTS_VOICE=ara" in text
    assert "SCREENSCRIBE_STT_LIVE_ENDPOINT=wss://api.x.ai/v1/stt" in text

    reloaded = ScreenScribeConfig()
    reloaded._load_from_file(path)
    assert reloaded.provider == "xai"
    assert reloaded.tts_voice == "ara"
    assert reloaded.stt_live_endpoint == "wss://api.x.ai/v1/stt"
    assert reloaded.stt_model == ""
