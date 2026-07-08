"""Configuration management with embedded defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .keywords import KeywordsConfig

# Default LibraxisAI configuration
LIBRAXIS_API_BASE = "https://api.libraxis.cloud"
LIBRAXIS_STT_ENDPOINT = f"{LIBRAXIS_API_BASE}/v1/audio/transcriptions"
LIBRAXIS_LLM_ENDPOINT = f"{LIBRAXIS_API_BASE}/v1/responses"
LIBRAXIS_VISION_ENDPOINT = f"{LIBRAXIS_API_BASE}/v1/responses"

# Default models
DEFAULT_STT_MODEL = "whisper-1"
DEFAULT_LLM_MODEL = "programmer"  # screenscribe product default (LibraxisAI profile)
# Vision shares the LLM model on the unified Responses endpoint; "ai-suggestions"
# is the LEGACY name and must not be the default. Override per-provider (e.g. an
# OpenAI vision model) via SCREENSCRIBE_VISION_MODEL when not on LibraxisAI.
DEFAULT_VISION_MODEL = "programmer"

# Config file locations (checked in order)
# User config has priority - local .env is for development/examples only
CONFIG_PATHS = [
    Path.home() / ".config" / "screenscribe" / "config.env",  # User config (primary)
    Path.home() / ".screenscribe.env",  # Alternative user config
    Path("/etc/screenscribe/config.env"),  # System-wide config
    # Note: Local .env is NOT auto-loaded - use env vars for overrides
]


def _mask_api_key(key: str) -> str:
    """Fully redact an API key for display.

    Returns a fixed-width mask with zero characters of the original key,
    so no prefix/suffix of the secret ever leaks into logs or terminal
    output. Empty/missing keys render as a clear NOT-SET marker.
    """
    if not key:
        return "[red]NOT SET[/]"
    return "*" * 24


@dataclass
class ScreenScribeConfig:
    """Screenscribe configuration."""

    # API Configuration (generic fallback)
    api_key: str = ""
    api_base: str = LIBRAXIS_API_BASE

    # Per-endpoint API keys (use these for multi-provider setups)
    stt_api_key: str = ""  # Falls back to api_key if empty
    llm_api_key: str = ""  # Falls back to api_key if empty
    vision_api_key: str = ""  # Falls back to api_key if empty

    # Endpoints
    stt_endpoint: str = LIBRAXIS_STT_ENDPOINT
    llm_endpoint: str = LIBRAXIS_LLM_ENDPOINT
    vision_endpoint: str = LIBRAXIS_VISION_ENDPOINT

    # Models
    stt_model: str = DEFAULT_STT_MODEL
    llm_model: str = DEFAULT_LLM_MODEL
    vision_model: str = DEFAULT_VISION_MODEL

    # Optional STT fallback (opt-in). The user supplies a second provider
    # (e.g. their own OpenAI key + endpoint); it is tried ONLY when the primary
    # STT endpoint fails. Empty = no fallback, primary only — never a silent
    # default, since a fallback routes the user's audio to another provider.
    stt_fallback_endpoint: str = ""
    stt_fallback_api_key: str = ""
    stt_fallback_model: str = ""

    # Processing options
    language: str = "en"
    use_vision_analysis: bool = True
    verbose: bool = False
    analysis_prompt_override: str = ""

    # Semantic LLM-merge pass (auto dedup of cross-category paraphrases) runs
    # after the cheap heuristic dedup. On by default; set SCREENSCRIBE_LLM_MERGE
    # to a falsey value to disable it (falls back to heuristic-only dedup, e.g.
    # when there is no LLM budget). A missing LLM API key also makes it a no-op.
    llm_merge_enabled: bool = True

    # Active keyword vocabulary hints (loaded from --keywords-file / global file /
    # built-in default). Passed to the AI as hints during detection and marker
    # analysis; never replaces the LLM. ``None`` means "not yet loaded" — callers
    # that need the active dictionary should use ``get_keywords()`` which lazily
    # loads the standard-priority dictionary. An empty dictionary is a safe no-op.
    keywords: "KeywordsConfig | None" = field(default=None, repr=False)

    def get_keywords(self) -> "KeywordsConfig":
        """Return the active keyword vocabulary, lazily loading defaults if unset.

        When a CLI command has already loaded keywords (e.g. honoring an explicit
        ``--keywords-file``) it sets ``config.keywords`` so this returns exactly
        that dictionary. When nothing was loaded, the standard-priority dictionary
        (global user file, else built-in default) is loaded on demand. Either way
        an empty dictionary is a safe no-op for prompt injection.
        """
        from .keywords import KeywordsConfig

        if self.keywords is None:
            self.keywords = KeywordsConfig.load()
        return self.keywords

    def get_stt_api_key(self) -> str:
        """Get API key for STT endpoint."""
        return self.stt_api_key or self.api_key

    def get_llm_api_key(self) -> str:
        """Get API key for LLM endpoint."""
        return self.llm_api_key or self.api_key

    def get_vision_api_key(self) -> str:
        """Get API key for Vision endpoint."""
        return self.vision_api_key or self.api_key

    def has_stt_fallback(self) -> bool:
        """True when a complete, opt-in STT fallback endpoint is configured."""
        return bool(self.stt_fallback_endpoint and self.stt_fallback_api_key)

    def get_stt_fallback_api_key(self) -> str:
        """API key for the optional STT fallback endpoint (no implicit fallback)."""
        return self.stt_fallback_api_key

    def get_stt_fallback_model(self) -> str:
        """Model for the STT fallback; defaults to OpenAI's whisper-1 when unset."""
        return self.stt_fallback_model or "whisper-1"

    def validate(self, providers: set[str] | None = None) -> list[str]:
        """Validate config and return list of warnings.

        ``providers`` scopes the check to the named providers ("llm", "vision",
        "stt"). When ``None`` (the default, used by ``review``) every provider is
        validated. ``analyze`` uses only vision (frame analysis) + stt (voice
        notes) and never contacts the LLM, so it passes ``{"vision", "stt"}`` --
        an unrelated/stale LLM key<->endpoint mismatch then no longer blocks a
        run that never touches the LLM (finding I).
        """
        if providers is None:
            providers = {"llm", "vision", "stt"}
        warnings = []

        # Check for endpoint/provider mismatch
        # Note: Both OpenAI and LibraxisAI support /v1/responses (Responses API)
        # LibraxisAI serves the OpenAI-compatible Responses API natively

        endpoint_by_provider = {"llm": self.llm_endpoint, "vision": self.vision_endpoint}
        libraxis_endpoints = [
            ep
            for provider, ep in endpoint_by_provider.items()
            if provider in providers and "libraxis" in ep
        ]
        for ep in libraxis_endpoints:
            if "/v1/chat/completions" in ep:
                warnings.append(
                    f"Invalid endpoint: {ep}\n"
                    "  LibraxisAI uses /v1/responses, not /v1/chat/completions\n"
                    "  Fix in: ~/.config/screenscribe/config.env"
                )

        # Key<->endpoint provider mismatch (P1-1). An OPENAI_API_KEY is mapped
        # onto llm_api_key/vision_api_key WITHOUT changing the endpoint, so an
        # OpenAI key can silently be sent to the default LibraxisAI endpoint (or
        # the reverse). This is a WARNING only: routing is never altered here and
        # the secret itself is never echoed.
        warnings.extend(self._key_endpoint_mismatch_warnings(providers))

        return warnings

    @staticmethod
    def _endpoint_provider(endpoint: str) -> str | None:
        """Best-effort provider tag from an endpoint host. None when unknown."""
        host = endpoint.lower()
        if "libraxis" in host:
            return "libraxis"
        if "openai.com" in host:
            return "openai"
        return None

    @staticmethod
    def _key_provider(key: str) -> str | None:
        """Best-effort provider tag from an API-key shape. None when ambiguous.

        Only the well-known OpenAI ``sk-`` prefix is inferred; anything else is
        ambiguous (could be LibraxisAI or a custom provider) and yields None so
        we never warn on a guess. The key value itself is never returned.
        """
        if key.startswith("sk-"):
            return "openai"
        return None

    def _key_endpoint_mismatch_warnings(self, providers: set[str]) -> list[str]:
        """Warn when a per-endpoint key's provider disagrees with its endpoint.

        Only the providers named in ``providers`` are checked, so a command can
        scope validation to the endpoints it actually uses (finding I).
        Pure detection — no re-routing, no hard block. Secrets are never echoed.
        """
        warnings: list[str] = []
        pairs = (
            ("llm", "LLM", self.get_llm_api_key(), self.llm_endpoint),
            ("vision", "Vision", self.get_vision_api_key(), self.vision_endpoint),
            ("stt", "STT", self.get_stt_api_key(), self.stt_endpoint),
        )
        for provider, label, key, endpoint in pairs:
            if provider not in providers:
                continue
            if not key:
                continue
            key_provider = self._key_provider(key)
            ep_provider = self._endpoint_provider(endpoint)
            if ep_provider is None:
                continue
            # Forward case (P1-1): an OpenAI `sk-` key on a non-OpenAI endpoint.
            # Reverse case: a non-`sk-` key on an OpenAI endpoint (which expects
            # the `sk-` shape). Both are real mismatches; ambiguous combos (e.g.
            # an unknown-shape key on a libraxis endpoint) are left silent.
            if key_provider == "openai" and ep_provider != "openai":
                detail = f"an openai API key is configured for the {ep_provider} endpoint"
            elif ep_provider == "openai" and key_provider != "openai":
                detail = "a non-openai API key is configured for the openai.com endpoint"
            else:
                continue
            env_endpoint_var = f"SCREENSCRIBE_{label.upper()}_ENDPOINT"
            env_key_var = f"SCREENSCRIBE_{label.upper()}_API_KEY"
            # Fail-closed (security): a provider mismatch would ship your key to
            # the wrong provider, so the run is BLOCKED until config is fixed.
            # The message names the exact key/endpoint pair and the two concrete
            # ways to resolve it -- the secret itself is never echoed.
            warnings.append(
                f"{label} key/endpoint mismatch -- this BLOCKS the run.\n"
                f"  What: {detail}\n"
                f"  Endpoint in use: {endpoint}\n"
                "  Why blocked: your API key would be sent to a provider it does "
                "not belong to.\n"
                "  Fix ONE of these in ~/.config/screenscribe/config.env (or as "
                "an env var):\n"
                f"    - point the endpoint at the key's provider: set {env_endpoint_var}\n"
                f"    - use the endpoint's matching key: set {env_key_var}"
            )
        return warnings

    @classmethod
    def load(cls) -> "ScreenScribeConfig":
        """Load config from environment and config files."""
        config = cls()

        # Try config files first
        for config_path in CONFIG_PATHS:
            if config_path.exists():
                config._load_from_file(config_path)
                break

        # Environment variables override config files
        config._load_from_env()

        return config

    def _load_from_file(self, path: Path) -> None:
        """Load configuration from .env file."""
        self._warn_if_world_readable(path)
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = self._parse_env_value(value)
                    self._set_from_key(key, value)

    @staticmethod
    def _parse_env_value(raw: str) -> str:
        """Parse the right-hand side of a ``KEY=value`` config line.

        Symmetric quotes: a value fully wrapped in matching ``'`` or ``"``
        quotes yields the inner text verbatim, and any trailing inline comment
        after the closing quote is dropped. Unquoted values have a trailing
        inline `` #`` comment stripped (a leading space is required, so a bare
        ``#`` inside a value -- e.g. a URL fragment or color -- is preserved).
        A stray, unmatched quote is left untouched so pre-existing values keep
        their exact meaning.
        """
        raw = raw.strip()
        if not raw:
            return ""
        quote = raw[0]
        if quote in ("'", '"'):
            end = raw.find(quote, 1)
            if end != -1:
                return raw[1:end]
            # Unterminated quote: fall through and treat the text literally.
        hash_idx = raw.find(" #")
        if hash_idx != -1:
            raw = raw[:hash_idx]
        return raw.strip()

    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        env_mapping = {
            # Generic API Key (fallback for all endpoints)
            "SCREENSCRIBE_API_KEY": "api_key",  # pragma: allowlist secret
            # Per-provider keys (set appropriate per-endpoint key)
            "LIBRAXIS_API_KEY": "stt_api_key",  # pragma: allowlist secret
            "OPENAI_API_KEY": "llm_api_key",  # pragma: allowlist secret
            # Explicit per-endpoint keys (highest priority)
            "SCREENSCRIBE_STT_API_KEY": "stt_api_key",  # pragma: allowlist secret
            "SCREENSCRIBE_LLM_API_KEY": "llm_api_key",  # pragma: allowlist secret
            "SCREENSCRIBE_VISION_API_KEY": "vision_api_key",  # pragma: allowlist secret
            # Base URL (auto-derives endpoints if explicit not set)
            "SCREENSCRIBE_API_BASE": "api_base",
            "LIBRAXIS_API_BASE": "api_base",
            # Explicit endpoints (full URLs, no normalization)
            "SCREENSCRIBE_STT_ENDPOINT": "stt_endpoint",
            "SCREENSCRIBE_LLM_ENDPOINT": "llm_endpoint",
            "SCREENSCRIBE_VISION_ENDPOINT": "vision_endpoint",
            # Optional STT fallback (opt-in second provider, tried on primary failure)
            "SCREENSCRIBE_STT_FALLBACK_ENDPOINT": "stt_fallback_endpoint",
            "SCREENSCRIBE_STT_FALLBACK_API_KEY": "stt_fallback_api_key",  # pragma: allowlist secret
            "SCREENSCRIBE_STT_FALLBACK_MODEL": "stt_fallback_model",
            # Models
            "SCREENSCRIBE_STT_MODEL": "stt_model",
            "SCREENSCRIBE_LLM_MODEL": "llm_model",
            "SCREENSCRIBE_VISION_MODEL": "vision_model",
            # Processing
            "SCREENSCRIBE_LANGUAGE": "language",
            "SCREENSCRIBE_VISION": "use_vision_analysis",
            "SCREENSCRIBE_LLM_MERGE": "llm_merge_enabled",
        }

        for env_key, attr in env_mapping.items():
            value = os.environ.get(env_key)
            if value:
                # Provider-generic env vars are convenient fallbacks, but they
                # should not clobber explicit per-endpoint screenscribe config
                # loaded from config.env. Use SCREENSCRIBE_*_API_KEY env vars
                # for an intentional per-endpoint override.
                if env_key == "OPENAI_API_KEY":
                    if not self.llm_api_key:
                        self.llm_api_key = value
                    if not self.vision_api_key:
                        self.vision_api_key = value
                elif env_key == "LIBRAXIS_API_KEY":
                    if not self.stt_api_key:
                        self.stt_api_key = value
                    if not self.api_key:
                        self.api_key = value
                else:
                    # Routing is taken straight from env_mapping (the mapped
                    # attribute is authoritative), never re-derived by substring
                    # matching on the key name. Editing a map value changes where
                    # the var lands -- unlike the old inert-value form (BH54).
                    self._assign_env_value(attr, value)

    # Attributes needing normalization when assigned from an env value; every
    # other mapped attribute is a plain string assignment.
    _ENDPOINT_ATTRS = (
        "stt_endpoint",
        "llm_endpoint",
        "vision_endpoint",
        "stt_fallback_endpoint",
    )
    _BOOL_ATTRS = ("use_vision_analysis", "llm_merge_enabled")

    def _assign_env_value(self, attr: str, value: str) -> None:
        """Assign an env value to the attribute env_mapping declares for it.

        The target field comes from the map (authoritative routing), not from
        substring-matching the key name. Only the per-attribute normalization is
        applied here -- the same transforms ``_set_from_key`` uses for the file
        path: endpoints drop a trailing slash, ``api_base`` normalizes and
        derives the default endpoints, and the two toggles parse truthy strings.
        """
        if attr == "api_base":
            self._apply_api_base(value)
        elif attr in self._ENDPOINT_ATTRS:
            setattr(self, attr, value.rstrip("/"))
        elif attr in self._BOOL_ATTRS:
            setattr(self, attr, value.lower() in ("true", "1", "yes"))
        else:
            setattr(self, attr, value)

    def _apply_api_base(self, value: str) -> None:
        """Normalize an API base URL and derive endpoints still at their defaults."""
        # Normalize api_base - remove trailing paths
        normalized = value.rstrip("/")
        for suffix in [
            "/v1/responses",
            "/v1/audio/transcriptions",
            "/v1/chat/completions",
            "/v1",
        ]:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        self.api_base = normalized
        # Only update endpoints if still at defaults (not explicitly set)
        if self.stt_endpoint == LIBRAXIS_STT_ENDPOINT:
            self.stt_endpoint = f"{normalized}/v1/audio/transcriptions"
        if self.llm_endpoint == LIBRAXIS_LLM_ENDPOINT:
            self.llm_endpoint = f"{normalized}/v1/responses"
        if self.vision_endpoint == LIBRAXIS_VISION_ENDPOINT:
            self.vision_endpoint = f"{normalized}/v1/responses"

    @staticmethod
    def _warn_if_world_readable(path: Path) -> None:
        """Warn (advisory, non-fatal) when a config file is group/other readable.

        The config holds the API key. ``save_default_config`` writes it 0600, but
        a hand-created file may be 0644 — readable by other local users. On POSIX
        we surface a UserWarning (``mode & 0o077``); Windows has no equivalent bit
        check, so this is a no-op there. Loading still proceeds.
        """
        if os.name != "posix":
            return
        try:
            mode = os.stat(path).st_mode
        except OSError:
            return
        if mode & 0o077:
            import warnings

            warnings.warn(
                f"Config file {path} is readable by other users "
                f"(permissions {mode & 0o777:o}). It holds your API key; "
                "tighten it with: chmod 600 "
                f"{path}",
                UserWarning,
                stacklevel=2,
            )

    def _set_from_key(self, key: str, value: str) -> None:
        """Set attribute from key-value pair."""
        key_lower = key.lower()

        # STT fallback (checked first: "stt_fallback_api_key" also contains the
        # generic "api_key" substring, so it must win before the broader checks).
        if "stt_fallback_api_key" in key_lower:
            self.stt_fallback_api_key = value
        elif "stt_fallback_endpoint" in key_lower:
            self.stt_fallback_endpoint = value.rstrip("/")
        elif "stt_fallback_model" in key_lower:
            self.stt_fallback_model = value
        # Per-endpoint API keys (explicit)
        elif "stt_api_key" in key_lower:
            self.stt_api_key = value
        elif "llm_api_key" in key_lower:
            self.llm_api_key = value
        elif "vision_api_key" in key_lower:
            self.vision_api_key = value
        # Provider-specific keys. These are GENERIC fallbacks, so they only fill
        # a slot that an explicit per-endpoint key has not already claimed --
        # matching the env loader (P2-B). Without the guard, an OPENAI_API_KEY
        # line could clobber an explicit vision/LLM key depending on the ORDER
        # of lines in config.env.
        elif "openai_api_key" in key_lower:
            # OpenAI key → LLM + Vision (never over an explicit key)
            if not self.llm_api_key:
                self.llm_api_key = value
            if not self.vision_api_key:
                self.vision_api_key = value
        elif "libraxis_api_key" in key_lower:
            # LibraxisAI key → STT + generic api_key (never over an explicit key)
            if not self.stt_api_key:
                self.stt_api_key = value
            if not self.api_key:
                self.api_key = value
        elif "api_key" in key_lower:
            self.api_key = value
        # Explicit endpoints (full URLs - use as-is, no normalization)
        elif "stt_endpoint" in key_lower:
            self.stt_endpoint = value.rstrip("/")
        elif "llm_endpoint" in key_lower:
            self.llm_endpoint = value.rstrip("/")
        elif "vision_endpoint" in key_lower:
            self.vision_endpoint = value.rstrip("/")
        # Base URL (derives endpoints if explicit not set)
        elif "api_base" in key_lower:
            self._apply_api_base(value)
        elif "stt_model" in key_lower:
            self.stt_model = value
        elif "llm_model" in key_lower:
            self.llm_model = value
        elif "vision_model" in key_lower:
            self.vision_model = value
        elif "language" in key_lower:
            self.language = value
        elif key_lower == "screenscribe_vision":
            # Exact match only: the boolean vision toggle maps from the declared
            # SCREENSCRIBE_VISION key alone. A catch-all `endswith("vision")`
            # here let any unrelated `*_VISION` key (incl. stray config.env
            # lines) misroute into this flag (BH54).
            self.use_vision_analysis = value.lower() in ("true", "1", "yes")
        elif key_lower == "screenscribe_llm_merge":
            # Exact match (same discipline as the vision toggle): the semantic
            # LLM-merge pass is enabled unless explicitly turned off.
            self.llm_merge_enabled = value.lower() in ("true", "1", "yes")

    def _emit_optional(self, var: str, value: str, placeholder: str) -> str:
        """Emit ``VAR=value`` when ``value`` is set, else a commented hint.

        Config regeneration must never silently drop a configured secret or
        endpoint: any non-empty field is written as an ACTIVE line so a
        subsequent load recovers it. Empty fields stay as documented, inert
        ``# VAR=<placeholder>`` hints.
        """
        if value:
            return f"{var}={value}"
        return f"# {var}={placeholder}"

    def save_default_config(self) -> Path:
        """Save the full config to the user's config directory.

        Every non-empty field is emitted as an active line (per-endpoint keys,
        the opt-in STT fallback triple, and a non-default ``api_base``), so a
        regeneration round-trips without losing configured values. Empty fields
        remain commented placeholders that document the available knobs.
        """
        config_dir = Path.home() / ".config" / "screenscribe"
        config_dir.mkdir(parents=True, exist_ok=True)
        try:  # owner-only dir on POSIX (no-op on Windows)
            config_dir.chmod(0o700)
        except OSError:
            pass
        config_path = config_dir / "config.env"

        sep = "# " + "=" * 77
        lines = [
            "# screenscribe configuration",
            "# Made with (งಠ_ಠ)ง by ⌜screenscribe⌟ © 2025-2026",
            "",
            sep,
            "# API KEY (required - pick one)",
            sep,
            "# Use any of these - first non-empty wins:",
            f"SCREENSCRIBE_API_KEY={self.api_key}",
            # Per-endpoint keys: only emitted actively when set (multi-provider
            # setups). Otherwise they stay commented so a plain single-key file
            # is not cluttered.
            self._emit_optional(
                "SCREENSCRIBE_STT_API_KEY", self.stt_api_key, "YOUR_STT_KEY"
            ),
            self._emit_optional(
                "SCREENSCRIBE_LLM_API_KEY", self.llm_api_key, "YOUR_LLM_KEY"
            ),
            self._emit_optional(
                "SCREENSCRIBE_VISION_API_KEY", self.vision_api_key, "YOUR_VISION_KEY"
            ),
            "# OPENAI_API_KEY=YOUR_OPENAI_KEY",
            "# LIBRAXIS_API_KEY=YOUR_LIBRAXIS_KEY",
            "",
            sep,
            "# ENDPOINTS (explicit full URLs - recommended for clarity)",
            sep,
            "# STT: Speech-to-Text (OpenAI Whisper compatible)",
            f"SCREENSCRIBE_STT_ENDPOINT={self.stt_endpoint}",
            "",
            "# Optional STT fallback (opt-in): a second provider tried ONLY if the primary",
            "# STT endpoint fails (e.g. rate limit / capacity). Set all three to enable.",
            "# Routes your audio to this provider on fallback, so it is off by default.",
            self._emit_optional(
                "SCREENSCRIBE_STT_FALLBACK_ENDPOINT",
                self.stt_fallback_endpoint,
                "https://api.openai.com/v1/audio/transcriptions",
            ),
            self._emit_optional(
                "SCREENSCRIBE_STT_FALLBACK_API_KEY",
                self.stt_fallback_api_key,
                "YOUR_OPENAI_KEY",
            ),
            self._emit_optional(
                "SCREENSCRIBE_STT_FALLBACK_MODEL",
                self.stt_fallback_model,
                "whisper-1",
            ),
            "",
            "# LLM: Language Model (Responses API - supports previous_response_id chaining)",
            f"SCREENSCRIBE_LLM_ENDPOINT={self.llm_endpoint}",
            "",
            "# Vision: Vision Model (same as LLM for unified APIs)",
            f"SCREENSCRIBE_VISION_ENDPOINT={self.vision_endpoint}",
            "",
            sep,
            "# ALTERNATIVE: Base URL (auto-derives endpoints with /v1/... paths)",
            sep,
        ]
        # api_base is emitted actively only when it diverges from the built-in
        # default; the explicit endpoints above already win on reload, so this
        # just preserves the operator's configured base for `config --show`.
        if self.api_base and self.api_base != LIBRAXIS_API_BASE:
            lines.append(f"SCREENSCRIBE_API_BASE={self.api_base}")
        else:
            lines.append("# SCREENSCRIBE_API_BASE=https://api.openai.com")
            lines.append("# SCREENSCRIBE_API_BASE=https://api.libraxis.cloud")
        lines += [
            "",
            sep,
            "# MODELS",
            sep,
            f"SCREENSCRIBE_STT_MODEL={self.stt_model}",
            f"SCREENSCRIBE_LLM_MODEL={self.llm_model}",
            f"SCREENSCRIBE_VISION_MODEL={self.vision_model}",
            "",
            sep,
            "# PROCESSING OPTIONS",
            sep,
            f"SCREENSCRIBE_LANGUAGE={self.language}",
            f"SCREENSCRIBE_VISION={str(self.use_vision_analysis).lower()}",
            "",
        ]
        content = "\n".join(lines)

        # The config holds the API key, so create it owner-only (0600). os.open
        # with the mode sets it at creation (no world-readable window for a new
        # file); the explicit chmod also tightens an already-existing config.
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:  # tighten an existing file too (no-op on Windows)
            os.chmod(config_path, 0o600)
        except OSError:
            pass

        return config_path

    def save_api_key(self, api_key: str) -> Path:
        """Persist a new primary API key without discarding other config.

        When a config file already exists, ONLY the ``SCREENSCRIBE_API_KEY``
        line is rewritten in place -- every other line (per-endpoint keys, the
        STT fallback, ``api_base``, and any user comments) survives byte-for-
        byte. A ``config.env.bak`` snapshot (0600) is taken first so the prior
        state is always recoverable. When no config file exists yet, the full
        default template is written (which now emits every non-empty field).
        """
        self.api_key = api_key
        config_path = Path.home() / ".config" / "screenscribe" / "config.env"
        if not config_path.exists():
            return self.save_default_config()
        self._backup_config(config_path)
        self._rewrite_api_key_line(config_path, api_key)
        return config_path

    @staticmethod
    def _backup_config(config_path: Path) -> Path:
        """Copy the current config to ``config.env.bak`` (owner-only 0600)."""
        backup_path = config_path.with_name(config_path.name + ".bak")
        data = config_path.read_bytes()
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:  # tighten an existing backup too (no-op on Windows)
            os.chmod(backup_path, 0o600)
        except OSError:
            pass
        return backup_path

    @staticmethod
    def _rewrite_api_key_line(config_path: Path, api_key: str) -> None:
        """Replace the active ``SCREENSCRIBE_API_KEY=`` line in place.

        Only the first uncommented ``SCREENSCRIBE_API_KEY`` line is rewritten
        (exact key match, so ``SCREENSCRIBE_STT_API_KEY`` and friends are never
        touched); the original newline style is preserved. When the file has no
        active line, one is appended.
        """
        lines = config_path.read_text().splitlines(keepends=True)
        new_value = f"SCREENSCRIBE_API_KEY={api_key}"
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.split("=", 1)[0].strip() == "SCREENSCRIBE_API_KEY":
                newline = line[len(line.rstrip("\r\n")) :] or "\n"
                lines[i] = new_value + newline
                replaced = True
                break
        if not replaced:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(new_value + "\n")
        content = "".join(lines)

        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:  # tighten an existing file too (no-op on Windows)
            os.chmod(config_path, 0o600)
        except OSError:
            pass
