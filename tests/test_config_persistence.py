"""Tests for config persistence: --set-key preservation, backups, template
full emission, and symmetric env-value parsing (W1-A1 config-persistence).
"""

import os
import stat
import sys
from pathlib import Path

import pytest

from screenscribe.config import LIBRAXIS_API_BASE, ScreenScribeConfig


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Path.home() at a temp dir and return the config.env path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path / ".config" / "screenscribe" / "config.env"


class TestSetKeyPreservesConfig:
    """`config --set-key` must not drop any other configured value."""

    def test_set_key_preserves_fallback_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "SCREENSCRIBE_API_KEY=old-key\n"  # pragma: allowlist secret
            "SCREENSCRIBE_STT_FALLBACK_ENDPOINT=https://api.openai.com/v1/audio/transcriptions\n"
            "SCREENSCRIBE_STT_FALLBACK_API_KEY=fallback-secret\n"  # pragma: allowlist secret
            "SCREENSCRIBE_STT_FALLBACK_MODEL=whisper-1\n"
        )

        cfg = ScreenScribeConfig()
        path = cfg.save_api_key("new-key")  # pragma: allowlist secret

        text = path.read_text()
        assert "SCREENSCRIBE_API_KEY=new-key" in text  # pragma: allowlist secret
        assert "old-key" not in text  # pragma: allowlist secret
        # Every other configured value survives.
        assert "SCREENSCRIBE_STT_FALLBACK_API_KEY=fallback-secret" in text  # pragma: allowlist secret
        assert (
            "SCREENSCRIBE_STT_FALLBACK_ENDPOINT=https://api.openai.com/v1/audio/transcriptions"
            in text
        )
        assert "SCREENSCRIBE_STT_FALLBACK_MODEL=whisper-1" in text

    def test_set_key_preserves_user_comments_byte_for_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        original = (
            "# my hand-written note\n"
            "SCREENSCRIBE_API_KEY=old-key\n"  # pragma: allowlist secret
            "SCREENSCRIBE_STT_API_KEY=stt-secret\n"  # pragma: allowlist secret
            "# trailing note kept\n"
        )
        config_path.write_text(original)

        cfg = ScreenScribeConfig()
        cfg.save_api_key("new-key")  # pragma: allowlist secret

        expected = original.replace("old-key", "new-key")  # pragma: allowlist secret
        assert config_path.read_text() == expected

    def test_set_key_appends_when_no_active_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("SCREENSCRIBE_STT_API_KEY=stt-secret\n")  # pragma: allowlist secret

        cfg = ScreenScribeConfig()
        cfg.save_api_key("added-key")  # pragma: allowlist secret

        text = config_path.read_text()
        assert "SCREENSCRIBE_STT_API_KEY=stt-secret" in text  # pragma: allowlist secret
        assert "SCREENSCRIBE_API_KEY=added-key" in text  # pragma: allowlist secret

    def test_set_key_only_touches_generic_key_not_per_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact-key match: SCREENSCRIBE_STT_API_KEY must be left alone."""
        config_path = _home(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "SCREENSCRIBE_STT_API_KEY=stt-secret\n"  # pragma: allowlist secret
            "SCREENSCRIBE_API_KEY=old-key\n"  # pragma: allowlist secret
        )

        cfg = ScreenScribeConfig()
        cfg.save_api_key("new-key")  # pragma: allowlist secret

        # Reload from the exact path (CONFIG_PATHS is import-time, unaffected by
        # the Path.home monkeypatch), same pattern as test_config_env.
        reloaded = ScreenScribeConfig()
        reloaded._load_from_file(config_path)
        assert reloaded.api_key == "new-key"  # pragma: allowlist secret
        assert reloaded.stt_api_key == "stt-secret"  # pragma: allowlist secret

    def test_set_key_on_missing_file_uses_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        assert not config_path.exists()

        cfg = ScreenScribeConfig()
        path = cfg.save_api_key("fresh-key")  # pragma: allowlist secret

        assert path == config_path
        assert "SCREENSCRIBE_API_KEY=fresh-key" in path.read_text()  # pragma: allowlist secret


class TestSetKeyBackup:
    """A backup snapshot is written before the in-place edit."""

    def test_backup_holds_prior_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        original = "SCREENSCRIBE_API_KEY=old-key\n"  # pragma: allowlist secret
        config_path.write_text(original)

        cfg = ScreenScribeConfig()
        cfg.save_api_key("new-key")  # pragma: allowlist secret

        backup_path = config_path.with_name("config.env.bak")
        assert backup_path.exists()
        assert backup_path.read_text() == original

    def test_no_backup_when_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)

        cfg = ScreenScribeConfig()
        cfg.save_api_key("fresh-key")  # pragma: allowlist secret

        assert not config_path.with_name("config.env.bak").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
    def test_both_files_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True)
        config_path.write_text("SCREENSCRIBE_API_KEY=old-key\n")  # pragma: allowlist secret

        cfg = ScreenScribeConfig()
        cfg.save_api_key("new-key")  # pragma: allowlist secret

        backup_path = config_path.with_name("config.env.bak")
        assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(backup_path).st_mode) == 0o600


class TestTemplateFullEmission:
    """save_default_config emits every non-empty field as an active line."""

    def test_emits_per_endpoint_and_fallback_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _home(monkeypatch, tmp_path)
        cfg = ScreenScribeConfig(
            api_key="main-key",  # pragma: allowlist secret
            stt_api_key="stt-key",  # pragma: allowlist secret
            stt_fallback_endpoint="https://api.openai.com/v1/audio/transcriptions",
            stt_fallback_api_key="fb-key",  # pragma: allowlist secret
            stt_fallback_model="whisper-1",
        )
        text = cfg.save_default_config().read_text()

        assert "SCREENSCRIBE_STT_API_KEY=stt-key" in text  # pragma: allowlist secret
        assert "SCREENSCRIBE_STT_FALLBACK_API_KEY=fb-key" in text  # pragma: allowlist secret
        assert "SCREENSCRIBE_STT_FALLBACK_MODEL=whisper-1" in text
        # An empty per-endpoint key stays commented, never an active blank line.
        assert "\nSCREENSCRIBE_LLM_API_KEY=" not in text
        assert "# SCREENSCRIBE_LLM_API_KEY=" in text

    def test_full_round_trip_preserves_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _home(monkeypatch, tmp_path)
        cfg = ScreenScribeConfig(
            api_key="main-key",  # pragma: allowlist secret
            stt_api_key="stt-key",  # pragma: allowlist secret
            stt_fallback_endpoint="https://api.openai.com/v1/audio/transcriptions",
            stt_fallback_api_key="fb-key",  # pragma: allowlist secret
            stt_fallback_model="whisper-1",
        )
        cfg.save_default_config()

        reloaded = ScreenScribeConfig()
        reloaded._load_from_file(config_path)
        assert reloaded.api_key == "main-key"  # pragma: allowlist secret
        assert reloaded.stt_api_key == "stt-key"  # pragma: allowlist secret
        assert reloaded.stt_fallback_api_key == "fb-key"  # pragma: allowlist secret
        assert reloaded.has_stt_fallback() is True

    def test_non_default_api_base_is_emitted_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _home(monkeypatch, tmp_path)
        cfg = ScreenScribeConfig(api_base="https://api.openai.com")
        text = cfg.save_default_config().read_text()
        assert "\nSCREENSCRIBE_API_BASE=https://api.openai.com" in text

    def test_default_api_base_stays_commented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _home(monkeypatch, tmp_path)
        cfg = ScreenScribeConfig(api_base=LIBRAXIS_API_BASE)
        text = cfg.save_default_config().read_text()
        assert "\nSCREENSCRIBE_API_BASE=" not in text
        assert "# SCREENSCRIBE_API_BASE=" in text


class TestEnvValueParsing:
    """Symmetric quote handling and inline-comment stripping in the loader."""

    def test_inline_comment_stripped(self) -> None:
        assert ScreenScribeConfig._parse_env_value("value # comment") == "value"

    def test_double_quoted_value(self) -> None:
        assert ScreenScribeConfig._parse_env_value('"quoted"') == "quoted"

    def test_single_quoted_value(self) -> None:
        assert ScreenScribeConfig._parse_env_value("'quoted'") == "quoted"

    def test_quoted_value_ignores_trailing_comment(self) -> None:
        assert ScreenScribeConfig._parse_env_value('"quoted" # note') == "quoted"

    def test_bare_hash_without_space_preserved(self) -> None:
        # A '#' with no leading space is a legal value character (fragment/color).
        assert ScreenScribeConfig._parse_env_value("value#frag") == "value#frag"

    def test_unmatched_quote_left_untouched(self) -> None:
        # Stray quote in the middle keeps prior semantics (acceptance criterion).
        assert ScreenScribeConfig._parse_env_value('v"x') == 'v"x'

    def test_empty_value(self) -> None:
        assert ScreenScribeConfig._parse_env_value("") == ""
        assert ScreenScribeConfig._parse_env_value("   ") == ""

    def test_inline_comment_via_file_loader(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text("SCREENSCRIBE_LLM_MODEL=custom # my note\n")
        config = ScreenScribeConfig()
        config._load_from_file(cfg_file)
        assert config.llm_model == "custom"
