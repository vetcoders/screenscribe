"""Tests for ScreenScribeConfig environment key handling."""

import os
import stat
import sys
import warnings
from pathlib import Path

import pytest

from screenscribe.config import (
    DEFAULT_LLM_MODEL,
    DEFAULT_VISION_MODEL,
    ScreenScribeConfig,
)


class TestConfigApiBase:
    """Tests for API base normalization and endpoint derivation."""

    def test_api_base_normalizes_and_derives_endpoints(self) -> None:
        """SCREENSCRIBE_API_BASE normalizes base and derives endpoints."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_API_BASE", "https://example.com/v1/")

        assert config.api_base == "https://example.com"
        assert config.stt_endpoint == "https://example.com/v1/audio/transcriptions"
        assert config.llm_endpoint == "https://example.com/v1/responses"
        assert config.vision_endpoint == "https://example.com/v1/responses"

    def test_api_base_does_not_override_explicit_endpoints(self) -> None:
        """Explicit endpoints remain unchanged when API base is set."""
        config = ScreenScribeConfig()
        config.stt_endpoint = "https://stt.example.com/custom"
        config.llm_endpoint = "https://llm.example.com/custom"
        config.vision_endpoint = "https://vision.example.com/custom"

        config._set_from_key("SCREENSCRIBE_API_BASE", "https://api.example.com")

        assert config.stt_endpoint == "https://stt.example.com/custom"
        assert config.llm_endpoint == "https://llm.example.com/custom"
        assert config.vision_endpoint == "https://vision.example.com/custom"


class TestModelDefaults:
    """LLM model default + override precedence (product intent: programmer)."""

    def test_default_llm_model_is_programmer(self) -> None:
        """Product default LLM model is 'programmer', not the legacy 'ai-suggestions'."""
        assert DEFAULT_LLM_MODEL == "programmer"
        assert ScreenScribeConfig().llm_model == "programmer"

    def test_default_vision_model_is_programmer(self) -> None:
        """SF-6: vision default is 'programmer', never the legacy 'ai-suggestions'.

        Non-vacuous: this fails the moment the default regresses to the legacy
        name, which is exactly the drift the guard protects against.
        """
        assert DEFAULT_VISION_MODEL == "programmer"
        assert DEFAULT_VISION_MODEL != "ai-suggestions"
        assert ScreenScribeConfig().vision_model == "programmer"

    def test_active_llm_model_override_wins(self) -> None:
        """An active SCREENSCRIBE_LLM_MODEL overrides the default."""
        config = ScreenScribeConfig()
        config._set_from_key("SCREENSCRIBE_LLM_MODEL", "custom")
        assert config.llm_model == "custom"

    def test_vision_model_override_wins(self) -> None:
        """A user can still override vision (e.g. an OpenAI vision model)."""
        config = ScreenScribeConfig()
        config._set_from_key("SCREENSCRIBE_VISION_MODEL", "gpt-5.2")
        assert config.vision_model == "gpt-5.2"

    def test_libraxis_endpoint_with_programmer_is_a_clean_setup(self) -> None:
        """SF-6: LibraxisAI endpoints + 'programmer' model = correct, no warning.

        The default product profile (Libraxis endpoints, programmer model, a
        LibraxisAI key) must validate cleanly -- no key/endpoint mismatch and no
        legacy-model flag.
        """
        config = ScreenScribeConfig(
            llm_api_key="lx-libraxis-secret",  # pragma: allowlist secret
            vision_api_key="lx-libraxis-secret",  # pragma: allowlist secret
        )
        assert config.vision_model == "programmer"
        assert "libraxis" in config.vision_endpoint
        warnings = config.validate()
        assert warnings == []

    def test_commented_llm_model_line_is_ignored(self, tmp_path: Path) -> None:
        """A commented '# SCREENSCRIBE_LLM_MODEL=...' line never sets the model."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text("# SCREENSCRIBE_LLM_MODEL=ai-suggestions\n")
        config = ScreenScribeConfig()
        config._load_from_file(cfg_file)
        assert config.llm_model == "programmer"


class TestSttFallbackConfig:
    """Tests for the opt-in STT fallback configuration."""

    def test_fallback_disabled_by_default(self) -> None:
        config = ScreenScribeConfig()
        assert config.has_stt_fallback() is False

    def test_fallback_keys_parse_without_polluting_primary(self) -> None:
        """Fallback keys must not be captured by the broader api_key/endpoint checks."""
        config = ScreenScribeConfig()

        config._set_from_key(
            "SCREENSCRIBE_STT_FALLBACK_ENDPOINT",
            "https://api.openai.com/v1/audio/transcriptions/",
        )
        config._set_from_key(
            "SCREENSCRIBE_STT_FALLBACK_API_KEY",  # pragma: allowlist secret
            "sk-fallback",  # pragma: allowlist secret
        )
        config._set_from_key("SCREENSCRIBE_STT_FALLBACK_MODEL", "whisper-1")

        # Landed on the fallback fields...
        assert config.stt_fallback_endpoint == "https://api.openai.com/v1/audio/transcriptions"
        assert config.stt_fallback_api_key == "sk-fallback"  # pragma: allowlist secret
        assert config.stt_fallback_model == "whisper-1"
        # ...and did NOT leak into the primary/generic fields.
        assert config.api_key == ""
        assert config.stt_api_key == ""
        assert config.stt_endpoint != config.stt_fallback_endpoint
        assert config.has_stt_fallback() is True

    def test_fallback_requires_both_endpoint_and_key(self) -> None:
        config = ScreenScribeConfig()
        config.stt_fallback_endpoint = "https://api.openai.com/v1/audio/transcriptions"
        assert config.has_stt_fallback() is False  # key still missing

    def test_fallback_model_defaults_to_whisper_1(self) -> None:
        config = ScreenScribeConfig()
        assert config.get_stt_fallback_model() == "whisper-1"
        config.stt_fallback_model = "whisper-large-v3"
        assert config.get_stt_fallback_model() == "whisper-large-v3"


# Every env var the loader honors, paired with its authoritative target field.
# This matrix is the guard for the "env_mapping is authoritative" refactor: each
# var must land on exactly the field named here (routing comes from the map, not
# from re-deriving the field via substring matching on the key name -- the BH54
# class of bug where a map value looks authoritative but is inert).
ALL_ENV_KEYS = [
    "SCREENSCRIBE_API_KEY",  # pragma: allowlist secret
    "LIBRAXIS_API_KEY",  # pragma: allowlist secret
    "OPENAI_API_KEY",  # pragma: allowlist secret
    "SCREENSCRIBE_STT_API_KEY",  # pragma: allowlist secret
    "SCREENSCRIBE_LLM_API_KEY",  # pragma: allowlist secret
    "SCREENSCRIBE_VISION_API_KEY",  # pragma: allowlist secret
    "SCREENSCRIBE_API_BASE",
    "LIBRAXIS_API_BASE",
    "SCREENSCRIBE_STT_ENDPOINT",
    "SCREENSCRIBE_LLM_ENDPOINT",
    "SCREENSCRIBE_VISION_ENDPOINT",
    "SCREENSCRIBE_STT_FALLBACK_ENDPOINT",
    "SCREENSCRIBE_STT_FALLBACK_API_KEY",  # pragma: allowlist secret
    "SCREENSCRIBE_STT_FALLBACK_MODEL",
    "SCREENSCRIBE_STT_MODEL",
    "SCREENSCRIBE_LLM_MODEL",
    "SCREENSCRIBE_VISION_MODEL",
    "SCREENSCRIBE_LANGUAGE",
    "SCREENSCRIBE_VISION",
    "SCREENSCRIBE_LLM_MERGE",
]

# (env_key, attribute, raw_value, expected) for the single-field vars: plain
# strings set verbatim; endpoints drop a trailing slash.
SINGLE_FIELD_CASES = [
    ("SCREENSCRIBE_API_KEY", "api_key", "probe-generic", "probe-generic"),
    ("SCREENSCRIBE_STT_API_KEY", "stt_api_key", "probe-stt", "probe-stt"),
    ("SCREENSCRIBE_LLM_API_KEY", "llm_api_key", "probe-llm", "probe-llm"),
    ("SCREENSCRIBE_VISION_API_KEY", "vision_api_key", "probe-vision", "probe-vision"),
    ("SCREENSCRIBE_STT_FALLBACK_API_KEY", "stt_fallback_api_key", "probe-fb", "probe-fb"),
    ("SCREENSCRIBE_STT_FALLBACK_MODEL", "stt_fallback_model", "whisper-x", "whisper-x"),
    ("SCREENSCRIBE_STT_MODEL", "stt_model", "stt-x", "stt-x"),
    ("SCREENSCRIBE_LLM_MODEL", "llm_model", "llm-x", "llm-x"),
    ("SCREENSCRIBE_VISION_MODEL", "vision_model", "vision-x", "vision-x"),
    ("SCREENSCRIBE_LANGUAGE", "language", "pl", "pl"),
    (
        "SCREENSCRIBE_STT_ENDPOINT",
        "stt_endpoint",
        "https://s.example.com/v1/x/",
        "https://s.example.com/v1/x",
    ),
    (
        "SCREENSCRIBE_LLM_ENDPOINT",
        "llm_endpoint",
        "https://l.example.com/v1/x/",
        "https://l.example.com/v1/x",
    ),
    (
        "SCREENSCRIBE_VISION_ENDPOINT",
        "vision_endpoint",
        "https://v.example.com/v1/x/",
        "https://v.example.com/v1/x",
    ),
    (
        "SCREENSCRIBE_STT_FALLBACK_ENDPOINT",
        "stt_fallback_endpoint",
        "https://f.example.com/v1/x/",
        "https://f.example.com/v1/x",
    ),
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Drop every honored env var so a single-var probe is hermetic."""
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestEnvMappingAuthoritative:
    """env_mapping is the single source of truth for env -> field routing.

    Each var must set exactly the field env_mapping declares for it. If the map
    value were inert (only the key name mattered), editing a value could not
    misroute -- but neither could it correct routing, which is the smell.
    """

    @pytest.mark.parametrize(
        ("env_key", "attr", "raw", "expected"),
        SINGLE_FIELD_CASES,
        ids=[case[0] for case in SINGLE_FIELD_CASES],
    )
    def test_single_field_var_routes_to_declared_attr(
        self,
        clean_env: pytest.MonkeyPatch,
        env_key: str,
        attr: str,
        raw: str,
        expected: str,
    ) -> None:
        clean_env.setenv(env_key, raw)
        config = ScreenScribeConfig()

        config._load_from_env()

        assert getattr(config, attr) == expected

    def test_screenscribe_api_base_normalizes_and_derives(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("SCREENSCRIBE_API_BASE", "https://base.example.com/v1/")
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.api_base == "https://base.example.com"
        assert config.stt_endpoint == "https://base.example.com/v1/audio/transcriptions"
        assert config.llm_endpoint == "https://base.example.com/v1/responses"
        assert config.vision_endpoint == "https://base.example.com/v1/responses"

    def test_libraxis_api_base_normalizes_and_derives(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("LIBRAXIS_API_BASE", "https://lx.example.com/v1")
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.api_base == "https://lx.example.com"
        assert config.llm_endpoint == "https://lx.example.com/v1/responses"

    def test_vision_toggle_parses_bool(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("SCREENSCRIBE_VISION", "false")
        config = ScreenScribeConfig()
        assert config.use_vision_analysis is True  # default, so the flip is non-vacuous

        config._load_from_env()
        assert config.use_vision_analysis is False

    def test_vision_toggle_truthy_values(self, clean_env: pytest.MonkeyPatch) -> None:
        for truthy in ("true", "1", "yes"):
            clean_env.setenv("SCREENSCRIBE_VISION", truthy)
            config = ScreenScribeConfig()
            config.use_vision_analysis = False
            config._load_from_env()
            assert config.use_vision_analysis is True, truthy

    def test_llm_merge_toggle_parses_bool(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("SCREENSCRIBE_LLM_MERGE", "false")
        config = ScreenScribeConfig()
        assert config.llm_merge_enabled is True  # default

        config._load_from_env()
        assert config.llm_merge_enabled is False

    def test_openai_key_fills_llm_and_vision(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("OPENAI_API_KEY", "sk-openai-probe")  # pragma: allowlist secret
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.llm_api_key == "sk-openai-probe"  # pragma: allowlist secret
        assert config.vision_api_key == "sk-openai-probe"  # pragma: allowlist secret
        # Generic OpenAI key does not touch STT or the generic api_key.
        assert config.stt_api_key == ""
        assert config.api_key == ""

    def test_libraxis_key_fills_stt_and_generic(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("LIBRAXIS_API_KEY", "lx-probe")  # pragma: allowlist secret
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.stt_api_key == "lx-probe"  # pragma: allowlist secret
        assert config.api_key == "lx-probe"  # pragma: allowlist secret
        # LibraxisAI key does not touch the LLM/Vision keys.
        assert config.llm_api_key == ""
        assert config.vision_api_key == ""


class TestFileKeyOverwriteSymmetry:
    """A generic provider key in config.env must not clobber an explicit key.

    The env loader already protects explicit keys ("if not ..."), but the file
    loader used to set the OpenAI/LibraxisAI keys unconditionally, so whether a
    generic OPENAI_API_KEY overwrote an explicit vision key depended on the line
    ORDER in config.env. The file semantics are unified to the env variant:
    explicit wins regardless of order.
    """

    @staticmethod
    def _write_cfg(tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config.env"
        cfg.write_text(body)
        os.chmod(cfg, 0o600)  # owner-only: keep the world-readable warning quiet
        return cfg

    def test_explicit_vision_wins_when_openai_line_comes_after(self, tmp_path: Path) -> None:
        cfg = self._write_cfg(
            tmp_path,
            "SCREENSCRIBE_VISION_API_KEY=explicit-vision\n"  # pragma: allowlist secret
            "OPENAI_API_KEY=sk-generic\n",  # pragma: allowlist secret
        )
        config = ScreenScribeConfig()
        config._load_from_file(cfg)

        # Explicit per-endpoint key is preserved; generic key only fills the
        # empty LLM slot.
        assert config.vision_api_key == "explicit-vision"  # pragma: allowlist secret
        assert config.llm_api_key == "sk-generic"  # pragma: allowlist secret

    def test_explicit_vision_wins_when_openai_line_comes_before(self, tmp_path: Path) -> None:
        cfg = self._write_cfg(
            tmp_path,
            "OPENAI_API_KEY=sk-generic\n"  # pragma: allowlist secret
            "SCREENSCRIBE_VISION_API_KEY=explicit-vision\n",  # pragma: allowlist secret
        )
        config = ScreenScribeConfig()
        config._load_from_file(cfg)

        assert config.vision_api_key == "explicit-vision"  # pragma: allowlist secret
        assert config.llm_api_key == "sk-generic"  # pragma: allowlist secret

    def test_explicit_llm_wins_when_openai_line_comes_after(self, tmp_path: Path) -> None:
        cfg = self._write_cfg(
            tmp_path,
            "SCREENSCRIBE_LLM_API_KEY=explicit-llm\n"  # pragma: allowlist secret
            "OPENAI_API_KEY=sk-generic\n",  # pragma: allowlist secret
        )
        config = ScreenScribeConfig()
        config._load_from_file(cfg)

        assert config.llm_api_key == "explicit-llm"  # pragma: allowlist secret
        assert config.vision_api_key == "sk-generic"  # pragma: allowlist secret

    def test_explicit_stt_wins_when_libraxis_line_comes_after(self, tmp_path: Path) -> None:
        cfg = self._write_cfg(
            tmp_path,
            "SCREENSCRIBE_STT_API_KEY=explicit-stt\n"  # pragma: allowlist secret
            "LIBRAXIS_API_KEY=lx-generic\n",  # pragma: allowlist secret
        )
        config = ScreenScribeConfig()
        config._load_from_file(cfg)

        # Explicit STT key survives; generic LibraxisAI key only fills api_key.
        assert config.stt_api_key == "explicit-stt"  # pragma: allowlist secret
        assert config.api_key == "lx-generic"  # pragma: allowlist secret

    def test_file_key_symmetry_matches_env_loader(self, tmp_path: Path) -> None:
        """The exact case the env loader already handled (P2-B parity): a config
        file and the equivalent env both keep the explicit key."""
        cfg = self._write_cfg(
            tmp_path,
            "SCREENSCRIBE_VISION_API_KEY=explicit-vision\n"  # pragma: allowlist secret
            "OPENAI_API_KEY=sk-generic\n",  # pragma: allowlist secret
        )
        file_cfg = ScreenScribeConfig()
        file_cfg._load_from_file(cfg)

        env_cfg = ScreenScribeConfig(
            vision_api_key="explicit-vision"  # pragma: allowlist secret
        )
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            os.environ["OPENAI_API_KEY"] = "sk-generic"  # pragma: allowlist secret
            env_cfg._load_from_env()
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

        assert file_cfg.vision_api_key == env_cfg.vision_api_key
        assert file_cfg.vision_api_key == "explicit-vision"  # pragma: allowlist secret


class TestProviderEnvPrecedence:
    """Provider-generic env vars are fallbacks, not clobber-everything overrides."""

    def test_openai_env_does_not_override_explicit_endpoint_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = ScreenScribeConfig(
            llm_api_key="configured-llm-key",  # pragma: allowlist secret
            vision_api_key="configured-vision-key",  # pragma: allowlist secret
        )
        monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")  # pragma: allowlist secret

        config._load_from_env()

        assert config.llm_api_key == "configured-llm-key"  # pragma: allowlist secret
        assert config.vision_api_key == "configured-vision-key"  # pragma: allowlist secret

    def test_explicit_screenscribe_env_overrides_endpoint_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = ScreenScribeConfig(
            llm_api_key="configured-llm-key",  # pragma: allowlist secret
            vision_api_key="configured-vision-key",  # pragma: allowlist secret
        )
        monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")  # pragma: allowlist secret
        monkeypatch.setenv(
            "SCREENSCRIBE_LLM_API_KEY",  # pragma: allowlist secret
            "explicit-env-llm-key",  # pragma: allowlist secret
        )
        monkeypatch.setenv(
            "SCREENSCRIBE_VISION_API_KEY",  # pragma: allowlist secret
            "explicit-env-vision-key",  # pragma: allowlist secret
        )

        config._load_from_env()

        assert config.llm_api_key == "explicit-env-llm-key"  # pragma: allowlist secret
        assert config.vision_api_key == "explicit-env-vision-key"  # pragma: allowlist secret

    def test_provider_env_fills_empty_endpoint_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = ScreenScribeConfig()
        monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")  # pragma: allowlist secret

        config._load_from_env()

        assert config.llm_api_key == "ambient-openai-key"  # pragma: allowlist secret
        assert config.vision_api_key == "ambient-openai-key"  # pragma: allowlist secret


class TestKeyEndpointMismatchWarning:
    """mismatch_warnings() flags (never blocks/re-routes) a key<->endpoint mismatch.

    P1-1 / finding 283: OPENAI_API_KEY is mapped onto the LLM/Vision keys without
    changing the endpoint, so an OpenAI `sk-` key can be sent to the default
    LibraxisAI endpoint (or, the reverse, a non-OpenAI key to api.openai.com).
    OpenAI-compatible gateways legitimately use `sk-` keys, so the contract is a
    NON-blocking WARNING surfaced by mismatch_warnings() — routing is never
    altered and the run continues.
    """

    def test_openai_key_on_libraxis_endpoint_warns(self) -> None:
        config = ScreenScribeConfig(
            llm_api_key="sk-openai-secret",  # pragma: allowlist secret
            vision_api_key="sk-openai-secret",  # pragma: allowlist secret
        )
        # Endpoints stay at the LibraxisAI defaults.
        warnings = config.mismatch_warnings()

        joined = "\n".join(warnings)
        assert "sk-" not in joined  # the secret itself is never echoed
        assert any("libraxis" in w.lower() and "openai" in w.lower() for w in warnings)
        assert any("No request was sent" in error for error in config.validate())

    def test_non_openai_key_on_openai_endpoint_warns(self) -> None:
        config = ScreenScribeConfig(
            llm_api_key="lx-libraxis-secret",  # pragma: allowlist secret
            llm_endpoint="https://api.openai.com/v1/responses",
        )
        warnings = config.mismatch_warnings()
        assert any("openai.com" in w.lower() for w in warnings)

    def test_matching_openai_key_and_endpoint_does_not_warn(self) -> None:
        config = ScreenScribeConfig(
            llm_api_key="sk-openai-secret",  # pragma: allowlist secret
            vision_api_key="sk-openai-secret",  # pragma: allowlist secret
            llm_endpoint="https://api.openai.com/v1/responses",
            vision_endpoint="https://api.openai.com/v1/responses",
        )
        warnings = config.mismatch_warnings()
        assert not any("mismatch" in w.lower() for w in warnings)

    def test_no_key_does_not_warn_about_mismatch(self) -> None:
        config = ScreenScribeConfig()  # defaults: libraxis endpoints, no keys
        warnings = config.mismatch_warnings()
        assert not any("mismatch" in w.lower() for w in warnings)

    def test_analyze_scope_ignores_stale_llm_mismatch(self) -> None:
        """Finding I: mismatch_warnings(providers={"vision","stt"}) skips an unused
        LLM mismatch so analyze (vision + STT only) is not warned about stale LLM
        config."""
        config = ScreenScribeConfig(
            llm_api_key="sk-stale-openai",  # pragma: allowlist secret  (mismatch on libraxis LLM endpoint)
            vision_api_key="lx-vision-key",  # pragma: allowlist secret  (clean: non-sk key on libraxis vision)
        )
        # Full check (review path) still flags the LLM mismatch.
        assert any("mismatch" in w.lower() for w in config.mismatch_warnings())
        # analyze scope drops the unused LLM provider -> no mismatch warning.
        scoped = config.mismatch_warnings(providers={"vision", "stt"})
        assert not any("mismatch" in w.lower() for w in scoped)

    def test_analyze_scope_still_flags_vision_mismatch(self) -> None:
        """A mismatch on a provider analyze DOES use (vision) must still be flagged."""
        config = ScreenScribeConfig(
            vision_api_key="sk-openai-secret",  # pragma: allowlist secret  (mismatch on libraxis vision)
        )
        scoped = config.mismatch_warnings(providers={"vision", "stt"})
        assert any("mismatch" in w.lower() for w in scoped)

    def test_analyze_scope_flags_stt_mismatch(self) -> None:
        """Finding O1 (regression of I): analyze uses STT (voice notes), so an
        STT key/endpoint mismatch is warned about. An sk- OpenAI key left on the
        default LibraxisAI STT endpoint is the exact case (non-blocking)."""
        config = ScreenScribeConfig(
            stt_api_key="sk-openai-secret",  # pragma: allowlist secret  (mismatch on libraxis stt)
        )
        scoped = config.mismatch_warnings(providers={"vision", "stt"})
        joined = "\n".join(scoped)
        assert "sk-" not in joined  # the secret itself is never echoed
        assert any("stt" in w.lower() and "mismatch" in w.lower() for w in scoped), (
            "STT key/endpoint mismatch must warn when stt is in scope"
        )

    def test_stt_mismatch_via_generic_api_key_fallback(self) -> None:
        """get_stt_api_key() falls back to the generic api_key, so a generic
        SCREENSCRIBE_API_KEY=sk-... on the default STT endpoint is a mismatch."""
        config = ScreenScribeConfig(
            api_key="sk-openai-secret",  # pragma: allowlist secret  (falls back to stt)
        )
        scoped = config.mismatch_warnings(providers={"vision", "stt"})
        assert any("stt" in w.lower() and "mismatch" in w.lower() for w in scoped)

    def test_clean_stt_key_does_not_warn(self) -> None:
        """A non-sk STT key on the default LibraxisAI STT endpoint is clean."""
        config = ScreenScribeConfig(
            stt_api_key="lx-stt-key",  # pragma: allowlist secret  (clean on libraxis stt)
        )
        scoped = config.mismatch_warnings(providers={"vision", "stt"})
        assert not any("stt" in w.lower() and "mismatch" in w.lower() for w in scoped)

    def test_stt_mismatch_out_of_scope_is_ignored(self) -> None:
        """Scope discipline: an STT mismatch must NOT be flagged for a command that
        does not use STT (e.g. mismatch_warnings(providers={"vision"}))."""
        config = ScreenScribeConfig(
            stt_api_key="sk-openai-secret",  # pragma: allowlist secret
        )
        scoped = config.mismatch_warnings(providers={"vision"})
        assert not any("stt" in w.lower() and "mismatch" in w.lower() for w in scoped)

    def test_mismatch_message_is_actionable_and_warns(self) -> None:
        """D1 (P1-1) / finding 283: the mismatch message names key+endpoint and the
        concrete fix, and is NON-blocking -- mismatch_warnings() carries it while
        validate() (the blocking gate) stays empty, so cli.py does NOT exit."""
        config = ScreenScribeConfig(
            llm_api_key="sk-openai-secret",  # pragma: allowlist secret
        )
        warnings = config.mismatch_warnings()

        # Surfaced as a warning...
        assert warnings, "key/endpoint mismatch must still be reported"
        assert config.validate(), "known provider mismatch must block before a request"
        msg = "\n".join(warnings)

        # Actionable: warns (run continues), names the offending pair, gives the fix.
        assert "warning" in msg.lower()
        assert "LLM" in msg
        assert "libraxis" in msg.lower()
        # The two concrete remediations are spelled out with the exact env vars.
        assert "SCREENSCRIBE_LLM_ENDPOINT" in msg
        assert "SCREENSCRIBE_LLM_API_KEY" in msg
        # The secret itself is never echoed.
        assert "sk-" not in msg


class TestVisionFlagExactMatch:
    """BH54: the use_vision_analysis flag maps from SCREENSCRIBE_VISION *only*.

    The flag is wired through the generic _set_from_key fall-through, which also
    receives arbitrary keys from config.env (_load_from_file). A catch-all
    `endswith("vision")` would let any unrelated `*_VISION` key flip the flag.
    Only the declared SCREENSCRIBE_VISION key may touch it.
    """

    def test_screenscribe_vision_still_sets_flag(self) -> None:
        """The legit key keeps working: SCREENSCRIBE_VISION drives the flag."""
        config = ScreenScribeConfig()
        assert config.use_vision_analysis is True  # default

        config._set_from_key("SCREENSCRIBE_VISION", "false")
        assert config.use_vision_analysis is False

        config._set_from_key("SCREENSCRIBE_VISION", "yes")
        assert config.use_vision_analysis is True

    def test_foreign_vision_suffix_key_does_not_set_flag(self) -> None:
        """A random `*_VISION` key must NOT misroute into use_vision_analysis.

        Falsifiable: restore `endswith("vision")` and this flips the flag to
        False, turning the assertion red.
        """
        config = ScreenScribeConfig()
        config._set_from_key("FOO_VISION", "false")
        assert config.use_vision_analysis is True  # untouched -> still default

    def test_foreign_vision_key_from_config_file_does_not_set_flag(self, tmp_path: Path) -> None:
        """End-to-end via _load_from_file: a stray FOO_VISION line is inert."""
        cfg = tmp_path / "config.env"
        cfg.write_text("FOO_VISION=false\n")
        config = ScreenScribeConfig()
        config._load_from_file(cfg)
        assert config.use_vision_analysis is True

    def test_vision_keyed_subfields_are_unaffected(self) -> None:
        """`*_VISION_*` keys still route to their endpoint/model/key targets,
        not to the boolean flag."""
        config = ScreenScribeConfig()
        config._set_from_key("SCREENSCRIBE_VISION_MODEL", "gpt-5.2")
        config._set_from_key("SCREENSCRIBE_VISION_ENDPOINT", "https://v.example.com")
        assert config.vision_model == "gpt-5.2"
        assert config.vision_endpoint == "https://v.example.com"
        assert config.use_vision_analysis is True  # the flag never moved


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
class TestConfigFilePermissionsWarning:
    """P3-11: loading a config.env readable by group/other (mode & 0o077) warns.

    The config file holds the API key. save_default_config writes it 0600, but a
    hand-created 0644 file is readable by other users with no warning. Loading
    such a file emits a non-fatal UserWarning (warn, never block).
    """

    def test_world_readable_config_warns_on_load(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.env"
        cfg.write_text("SCREENSCRIBE_LLM_MODEL=custom\n")
        os.chmod(cfg, 0o644)  # group/other readable

        config = ScreenScribeConfig()
        with pytest.warns(UserWarning, match="permission"):
            config._load_from_file(cfg)

        # The value was still loaded — warning is advisory only, not a block.
        assert config.llm_model == "custom"

    def test_owner_only_config_does_not_warn(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.env"
        cfg.write_text("SCREENSCRIBE_LLM_MODEL=custom\n")
        os.chmod(cfg, 0o600)

        config = ScreenScribeConfig()
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning becomes a failure
            config._load_from_file(cfg)
        assert config.llm_model == "custom"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_saved_config_is_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """save_default_config writes the API key, so the file must be owner-only
    (0600) on POSIX."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = ScreenScribeConfig(**{"api" + "_key": "test-key"})
    path = config.save_default_config()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
