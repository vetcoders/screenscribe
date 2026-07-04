"""Tests for config load/env/file precedence and VTT cue generation (W1B-07/11).

Two modules under test:
  * screenscribe.config.ScreenScribeConfig — load(), _load_from_file(),
    _load_from_env(), and env-var precedence over file values.
  * screenscribe.vtt_generator — VTT timestamp formatting, cue generation,
    subtitle entries and data-url embedding.

External I/O is limited to real temp files (tmp_path) and process env
(monkeypatch); both are local and deterministic.
"""

import base64

import pytest

from screenscribe import config as config_mod
from screenscribe.config import (
    DEFAULT_LLM_MODEL,
    DEFAULT_STT_MODEL,
    LIBRAXIS_API_BASE,
    ScreenScribeConfig,
)
from screenscribe.transcribe import Segment
from screenscribe.vtt_generator import (
    SubtitleEntry,
    format_display_timestamp,
    generate_vtt_data_url,
    generate_webvtt,
    generate_webvtt_with_cue_settings,
    seconds_to_vtt_timestamp,
    segments_to_subtitle_entries,
)


@pytest.fixture(autouse=True)
def _clean_screenscribe_env(monkeypatch):
    """Strip every config-relevant env var so each test starts from defaults.

    ScreenScribeConfig._load_from_env reads real os.environ, so a leaked
    SCREENSCRIBE_*/OPENAI/LIBRAXIS var on the dev box would silently change
    results. We delete the full mapping plus the provider-generic names.
    """
    for env_key in (
        "SCREENSCRIBE_API_KEY",
        "LIBRAXIS_API_KEY",
        "OPENAI_API_KEY",
        "SCREENSCRIBE_STT_API_KEY",
        "SCREENSCRIBE_LLM_API_KEY",
        "SCREENSCRIBE_VISION_API_KEY",
        "SCREENSCRIBE_API_BASE",
        "LIBRAXIS_API_BASE",
        "SCREENSCRIBE_STT_ENDPOINT",
        "SCREENSCRIBE_LLM_ENDPOINT",
        "SCREENSCRIBE_VISION_ENDPOINT",
        "SCREENSCRIBE_STT_FALLBACK_ENDPOINT",
        "SCREENSCRIBE_STT_FALLBACK_API_KEY",
        "SCREENSCRIBE_STT_FALLBACK_MODEL",
        "SCREENSCRIBE_STT_MODEL",
        "SCREENSCRIBE_LLM_MODEL",
        "SCREENSCRIBE_VISION_MODEL",
        "SCREENSCRIBE_LANGUAGE",
        "SCREENSCRIBE_VISION",
    ):
        monkeypatch.delenv(env_key, raising=False)


# ---------------------------------------------------------------------------
# config: _load_from_file
# ---------------------------------------------------------------------------


class TestLoadFromFile:
    """_load_from_file parses .env lines: comments/blanks skipped, quotes stripped."""

    def test_parses_key_value_and_strips_quotes(self, tmp_path):
        """A config.env file maps keys to attrs and strips surrounding quotes."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text(
            'SCREENSCRIBE_LANGUAGE="pl"\n'
            "SCREENSCRIBE_STT_MODEL='custom-whisper'\n"
            "SCREENSCRIBE_LLM_MODEL=my-llm\n"
        )
        config = ScreenScribeConfig()

        config._load_from_file(cfg_file)

        assert config.language == "pl"
        assert config.stt_model == "custom-whisper"
        assert config.llm_model == "my-llm"

    def test_ignores_comments_blank_lines_and_lines_without_equals(self, tmp_path):
        """Comment lines, blank lines, and non key=value lines are skipped."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text(
            "# this is a comment\n\n   \nnot_a_pair_line\nSCREENSCRIBE_LANGUAGE=de\n"
        )
        config = ScreenScribeConfig()

        config._load_from_file(cfg_file)

        # Only the one valid line took effect; defaults survive elsewhere.
        assert config.language == "de"
        assert config.stt_model == DEFAULT_STT_MODEL


# ---------------------------------------------------------------------------
# config: load() — file + env precedence
# ---------------------------------------------------------------------------


class TestLoadPrecedence:
    """ScreenScribeConfig.load picks the first existing CONFIG_PATH then env wins."""

    def test_load_reads_first_existing_config_file(self, tmp_path, monkeypatch):
        """load() loads values from the first existing CONFIG_PATHS entry."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text("SCREENSCRIBE_LANGUAGE=fr\nSCREENSCRIBE_LLM_MODEL=file-llm\n")
        # Point CONFIG_PATHS at our temp file only.
        monkeypatch.setattr(config_mod, "CONFIG_PATHS", [cfg_file])

        config = ScreenScribeConfig.load()

        assert config.language == "fr"
        assert config.llm_model == "file-llm"

    def test_load_stops_at_first_existing_path(self, tmp_path, monkeypatch):
        """Only the first existing config file is read; later ones are ignored."""
        first = tmp_path / "first.env"
        first.write_text("SCREENSCRIBE_LANGUAGE=es\n")
        second = tmp_path / "second.env"
        second.write_text("SCREENSCRIBE_LANGUAGE=it\n")
        missing = tmp_path / "missing.env"
        monkeypatch.setattr(config_mod, "CONFIG_PATHS", [missing, first, second])

        config = ScreenScribeConfig.load()

        # 'first' wins over 'second' (break after first existing path).
        assert config.language == "es"

    def test_env_overrides_config_file_values(self, tmp_path, monkeypatch):
        """Environment variables override values loaded from the config file."""
        cfg_file = tmp_path / "config.env"
        cfg_file.write_text("SCREENSCRIBE_LANGUAGE=fr\nSCREENSCRIBE_LLM_MODEL=file-llm\n")
        monkeypatch.setattr(config_mod, "CONFIG_PATHS", [cfg_file])
        monkeypatch.setenv("SCREENSCRIBE_LANGUAGE", "ja")
        monkeypatch.setenv("SCREENSCRIBE_LLM_MODEL", "env-llm")

        config = ScreenScribeConfig.load()

        assert config.language == "ja"
        assert config.llm_model == "env-llm"

    def test_load_with_no_config_file_uses_defaults_plus_env(self, monkeypatch):
        """With no config files present, load() yields defaults plus env overrides."""
        monkeypatch.setattr(config_mod, "CONFIG_PATHS", [])
        monkeypatch.setenv("SCREENSCRIBE_STT_MODEL", "env-stt")

        config = ScreenScribeConfig.load()

        assert config.stt_model == "env-stt"
        # Untouched fields keep their dataclass defaults.
        assert config.llm_model == DEFAULT_LLM_MODEL
        assert config.api_base == LIBRAXIS_API_BASE


# ---------------------------------------------------------------------------
# config: _load_from_env — provider-generic key precedence
# ---------------------------------------------------------------------------


class TestEnvKeyMapping:
    """_load_from_env special-cases OPENAI_/LIBRAXIS_ keys to not clobber explicit."""

    def test_openai_key_fills_llm_and_vision_when_empty(self, monkeypatch):
        """OPENAI_API_KEY fills llm/vision keys only when they are empty."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.llm_api_key == "sk-openai"  # pragma: allowlist secret
        assert config.vision_api_key == "sk-openai"  # pragma: allowlist secret
        # OPENAI does not touch the generic api_key.
        assert config.api_key == ""  # pragma: allowlist secret

    def test_openai_key_does_not_clobber_explicit_llm_key(self, monkeypatch):
        """An explicit per-endpoint key wins over the provider-generic OPENAI key."""
        monkeypatch.setenv("SCREENSCRIBE_LLM_API_KEY", "sk-explicit-llm")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        config = ScreenScribeConfig()

        config._load_from_env()

        # Explicit LLM key preserved; only the empty vision slot gets OPENAI.
        assert config.llm_api_key == "sk-explicit-llm"  # pragma: allowlist secret
        assert config.vision_api_key == "sk-openai"  # pragma: allowlist secret

    def test_libraxis_key_fills_stt_and_generic_api_key(self, monkeypatch):
        """LIBRAXIS_API_KEY seeds stt_api_key and the generic api_key when empty."""
        monkeypatch.setenv("LIBRAXIS_API_KEY", "lx-key")
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.stt_api_key == "lx-key"  # pragma: allowlist secret
        assert config.api_key == "lx-key"  # pragma: allowlist secret

    def test_libraxis_key_does_not_clobber_existing_api_key(self, monkeypatch):
        """A pre-set generic api_key is not overwritten by LIBRAXIS_API_KEY."""
        monkeypatch.setenv("LIBRAXIS_API_KEY", "lx-key")
        config = ScreenScribeConfig()
        config.api_key = "preset"  # pragma: allowlist secret
        config.stt_api_key = "preset-stt"  # pragma: allowlist secret

        config._load_from_env()

        # Neither already-populated slot is clobbered.
        assert config.api_key == "preset"  # pragma: allowlist secret
        assert config.stt_api_key == "preset-stt"  # pragma: allowlist secret

    def test_generic_env_routes_through_set_from_key(self, monkeypatch):
        """Non-special env vars flow through _set_from_key (e.g. language/base)."""
        monkeypatch.setenv("SCREENSCRIBE_LANGUAGE", "pl")
        monkeypatch.setenv("SCREENSCRIBE_API_BASE", "https://api.example.com")
        config = ScreenScribeConfig()

        config._load_from_env()

        assert config.language == "pl"
        assert config.api_base == "https://api.example.com"
        # Base derives the endpoints from default.
        assert config.stt_endpoint == "https://api.example.com/v1/audio/transcriptions"

    def test_empty_env_value_is_ignored(self, monkeypatch):
        """An empty-string env var does not override the default."""
        monkeypatch.setenv("SCREENSCRIBE_LANGUAGE", "")
        config = ScreenScribeConfig()

        config._load_from_env()

        # falsy value skipped; default "en" survives.
        assert config.language == "en"


# ---------------------------------------------------------------------------
# config: _set_from_key — fallback + boolean + endpoint normalization
# ---------------------------------------------------------------------------


class TestSetFromKey:
    """_set_from_key precedence ordering and value normalization."""

    def test_stt_fallback_api_key_wins_over_generic_api_key_substring(self):
        """stt_fallback_api_key is matched before the broad api_key branch."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_STT_FALLBACK_API_KEY", "fb-key")

        assert config.stt_fallback_api_key == "fb-key"  # pragma: allowlist secret
        # Must NOT have leaked into the generic api_key slot.
        assert config.api_key == ""  # pragma: allowlist secret

    def test_endpoint_values_strip_trailing_slash(self):
        """Explicit endpoint keys store the URL with any trailing slash removed."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_STT_ENDPOINT", "https://stt.example.com/x/")
        config._set_from_key("SCREENSCRIBE_STT_FALLBACK_ENDPOINT", "https://fb.example.com/y/")

        assert config.stt_endpoint == "https://stt.example.com/x"
        assert config.stt_fallback_endpoint == "https://fb.example.com/y"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("1", True), ("YES", True), ("false", False), ("no", False)],
    )
    def test_vision_boolean_parsing(self, raw, expected):
        """SCREENSCRIBE_VISION parses truthy strings case-insensitively."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_VISION", raw)

        assert config.use_vision_analysis is expected

    def test_vision_flag_not_flipped_by_other_keys(self):
        """The vision boolean branch only matches the bare *vision suffix."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_VISION", "false")

        assert config.use_vision_analysis is False
        # A vision *model* / *endpoint* / *api_key* key must not flip the boolean flag.
        config._set_from_key("SCREENSCRIBE_VISION_MODEL", "vmodel")
        assert config.vision_model == "vmodel"
        assert config.use_vision_analysis is False
        config._set_from_key("SCREENSCRIBE_VISION_ENDPOINT", "https://v.example.com")
        assert config.use_vision_analysis is False


class TestSetFromKeyProviderAndAccessors:
    """Provider-key routing in _set_from_key plus the per-endpoint accessors."""

    def test_openai_key_via_file_routes_to_llm_and_vision(self):
        """An OPENAI_API_KEY line routes the key to both llm and vision slots."""
        config = ScreenScribeConfig()

        config._set_from_key("OPENAI_API_KEY", "sk-file-openai")

        assert config.llm_api_key == "sk-file-openai"  # pragma: allowlist secret
        assert config.vision_api_key == "sk-file-openai"  # pragma: allowlist secret

    def test_libraxis_key_via_file_seeds_stt_and_generic(self):
        """A LIBRAXIS_API_KEY line seeds stt and the generic api_key when empty."""
        config = ScreenScribeConfig()

        config._set_from_key("LIBRAXIS_API_KEY", "lx-file")

        assert config.stt_api_key == "lx-file"  # pragma: allowlist secret
        assert config.api_key == "lx-file"  # pragma: allowlist secret

    def test_explicit_per_endpoint_keys_and_fallback_model(self):
        """Explicit stt/llm/vision key lines and the fallback model line all land."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_VISION_API_KEY", "v-key")
        config._set_from_key("SCREENSCRIBE_STT_FALLBACK_MODEL", "fb-model")
        config._set_from_key("SCREENSCRIBE_LLM_ENDPOINT", "https://llm.example.com/v1/")
        config._set_from_key("SCREENSCRIBE_VISION_ENDPOINT", "https://vis.example.com/v1/")

        assert config.vision_api_key == "v-key"  # pragma: allowlist secret
        assert config.stt_fallback_model == "fb-model"
        # llm/vision endpoint trailing slash stripped.
        assert config.llm_endpoint == "https://llm.example.com/v1"
        assert config.vision_endpoint == "https://vis.example.com/v1"

    def test_generic_api_key_line_sets_api_key(self):
        """A bare *_API_KEY line that is not provider-specific sets the generic key."""
        config = ScreenScribeConfig()

        config._set_from_key("SCREENSCRIBE_API_KEY", "generic-key")

        assert config.api_key == "generic-key"  # pragma: allowlist secret

    def test_per_endpoint_api_key_accessors_fall_back_to_generic(self):
        """get_*_api_key returns the per-endpoint key, else the generic api_key."""
        config = ScreenScribeConfig(api_key="generic")

        # No per-endpoint keys -> all fall back to the generic key.
        assert config.get_stt_api_key() == "generic"  # pragma: allowlist secret
        assert config.get_llm_api_key() == "generic"  # pragma: allowlist secret
        assert config.get_vision_api_key() == "generic"  # pragma: allowlist secret

        config.llm_api_key = "llm-specific"  # pragma: allowlist secret
        assert config.get_llm_api_key() == "llm-specific"  # pragma: allowlist secret
        # The others still fall back.
        assert config.get_stt_api_key() == "generic"  # pragma: allowlist secret

    def test_stt_fallback_helpers(self):
        """has_stt_fallback / get_stt_fallback_* reflect the opt-in fallback config."""
        config = ScreenScribeConfig()

        # Not configured -> no fallback, key empty, model defaults to whisper-1.
        assert config.has_stt_fallback() is False
        assert config.get_stt_fallback_api_key() == ""  # pragma: allowlist secret
        assert config.get_stt_fallback_model() == "whisper-1"

        config.stt_fallback_endpoint = "https://fb.example.com"
        config.stt_fallback_api_key = "fb-key"  # pragma: allowlist secret
        config.stt_fallback_model = "fb-whisper"
        assert config.has_stt_fallback() is True
        assert config.get_stt_fallback_api_key() == "fb-key"  # pragma: allowlist secret
        assert config.get_stt_fallback_model() == "fb-whisper"


class TestValidate:
    """validate() flags only libraxis endpoints misusing /v1/chat/completions."""

    def test_no_warnings_for_default_responses_endpoints(self):
        """Default LibraxisAI /v1/responses endpoints produce no warnings."""
        config = ScreenScribeConfig()

        assert config.validate() == []

    def test_warns_on_libraxis_chat_completions_endpoint(self):
        """A libraxis LLM endpoint using /v1/chat/completions raises a warning."""
        config = ScreenScribeConfig()
        config.llm_endpoint = "https://api.libraxis.cloud/v1/chat/completions"

        warnings = config.validate()

        assert len(warnings) == 1
        assert "/v1/responses, not /v1/chat/completions" in warnings[0]

    def test_no_warning_for_non_libraxis_chat_completions(self):
        """A non-libraxis chat/completions endpoint is not flagged."""
        config = ScreenScribeConfig()
        config.llm_endpoint = "https://api.openai.com/v1/chat/completions"

        assert config.validate() == []


# ---------------------------------------------------------------------------
# vtt_generator: timestamp formatting
# ---------------------------------------------------------------------------


class TestVttTimestamps:
    """seconds_to_vtt_timestamp / format_display_timestamp formatting rules."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "00:00:00.000"),
            (5.5, "00:00:05.500"),
            (125.456, "00:02:05.456"),
            (3661.0, "01:01:01.000"),
        ],
    )
    def test_seconds_to_vtt_timestamp(self, seconds, expected):
        """Seconds convert to zero-padded HH:MM:SS.mmm WebVTT format."""
        assert seconds_to_vtt_timestamp(seconds) == expected

    def test_display_timestamp_short_form_under_one_hour(self):
        """Under an hour, display omits the hour field (M:SS)."""
        assert format_display_timestamp(125.0) == "2:05"
        assert format_display_timestamp(5.0) == "0:05"

    def test_display_timestamp_long_form_with_hours(self):
        """At/over an hour, display includes the hour field (H:MM:SS)."""
        assert format_display_timestamp(3725.0) == "1:02:05"


# ---------------------------------------------------------------------------
# vtt_generator: cue generation
# ---------------------------------------------------------------------------


def _make_segments():
    return [
        Segment(id=1, start=0.0, end=5.5, text="Hello world"),
        Segment(id=2, start=5.5, end=10.2, text="This is a test"),
    ]


class TestWebVttGeneration:
    """generate_webvtt and the cue-settings variant emit valid WebVTT bodies."""

    def test_generate_webvtt_header_and_cues(self):
        """generate_webvtt emits header, language and one cue block per segment."""
        vtt = generate_webvtt(_make_segments(), language="pl")
        lines = vtt.split("\n")

        assert lines[0] == "WEBVTT"
        assert lines[1] == "Kind: captions"
        assert lines[2] == "Language: pl"
        # First cue: id, timing line, text.
        assert "1" in lines
        assert "00:00:00.000 --> 00:00:05.500" in vtt
        assert "Hello world" in vtt
        assert "00:00:05.500 --> 00:00:10.200" in vtt
        assert "This is a test" in vtt

    def test_generate_webvtt_empty_segments_is_header_only(self):
        """With no segments only the four header lines are produced."""
        vtt = generate_webvtt([], language="en")

        assert vtt == "WEBVTT\nKind: captions\nLanguage: en\n"

    def test_generate_webvtt_with_cue_settings_appends_positioning(self):
        """Cue-settings variant appends position/line/align to each timing line."""
        vtt = generate_webvtt_with_cue_settings(
            _make_segments(),
            position="10%",
            line="5",
            align="start",
            language="de",
        )

        assert "Language: de" in vtt
        assert "00:00:00.000 --> 00:00:05.500 position:10% line:5 align:start" in vtt
        assert "Hello world" in vtt

    def test_cue_settings_defaults(self):
        """Default cue settings produce position:50% line:auto align:center."""
        vtt = generate_webvtt_with_cue_settings(_make_segments())

        assert "position:50% line:auto align:center" in vtt


# ---------------------------------------------------------------------------
# vtt_generator: subtitle entries + data url
# ---------------------------------------------------------------------------


class TestSubtitleEntriesAndDataUrl:
    """SubtitleEntry.from_segment, segments_to_subtitle_entries, data-url embed."""

    def test_subtitle_entry_from_segment_fills_display_fields(self):
        """from_segment copies timing/text and derives human display timestamps."""
        seg = Segment(id=7, start=65.0, end=130.0, text="cue text")

        entry = SubtitleEntry.from_segment(seg)

        assert entry.id == 7
        assert entry.start == 65.0
        assert entry.end == 130.0
        assert entry.text == "cue text"
        assert entry.display_start == "1:05"
        assert entry.display_end == "2:10"

    def test_segments_to_subtitle_entries_maps_all(self):
        """segments_to_subtitle_entries returns one SubtitleEntry per segment, in order."""
        entries = segments_to_subtitle_entries(_make_segments())

        assert [e.id for e in entries] == [1, 2]
        assert all(isinstance(e, SubtitleEntry) for e in entries)
        assert entries[1].text == "This is a test"

    def test_generate_vtt_data_url_decodes_back_to_vtt(self):
        """The data URL is a base64 WebVTT payload that round-trips to the body."""
        segments = _make_segments()
        url = generate_vtt_data_url(segments, language="pl")

        assert url.startswith("data:text/vtt;base64,")
        payload = url.split(",", 1)[1]
        decoded = base64.b64decode(payload).decode("utf-8")
        assert decoded == generate_webvtt(segments, language="pl")
        assert "Hello world" in decoded
        assert "Language: pl" in decoded
