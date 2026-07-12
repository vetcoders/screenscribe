"""Model validation - fail fast before pipeline starts."""

from typing import Any

import httpx
from rich.console import Console

from screenscribe.api_utils import is_chat_completions_endpoint
from screenscribe.config import ScreenScribeConfig

console = Console()

# Validation timeout - short, just checking availability
VALIDATION_TIMEOUT = 10.0
_VALIDATION_IMAGE_DATA_URL = (
    "data:image/gif;base64,R0lGODlhAQABAIABAP///wAAACwAAAAAAQABAAACAkQBADs="
)


class ModelValidationError(Exception):
    """Raised when model validation fails."""

    def __init__(self, message: str, model_type: str, model_name: str) -> None:
        super().__init__(message)
        self.model_type = model_type
        self.model_name = model_name


class APIKeyError(Exception):
    """Raised when API key is missing or invalid."""


def _check_llm_model(config: ScreenScribeConfig, model: str, model_type: str) -> bool:
    """Check if LLM/Vision model is available via minimal request.

    Returns True when the probe confirms the model works. Returns False when the
    probe ran but the provider reported a failure (or the result is unclear): an
    HONEST, non-blocking outcome -- validation is PRE-FLIGHT and the model may
    still work downstream, so we never hard-block a possibly-working model on a
    probe hiccup, but we also never report a silent green check for a probe that
    actually failed. Raises on definitive failures (bad key, model not found,
    cannot connect).
    """
    is_vision = model_type == "Vision"
    endpoint = config.vision_endpoint if is_vision else config.llm_endpoint
    api_key = config.get_vision_api_key() if is_vision else config.get_llm_api_key()

    # Same guard as STT: a remote endpoint with no key would send
    # `Authorization: Bearer ` (empty) and crash deep in the HTTP stack (h11
    # rejects the empty value) as a raw traceback. Fail clearly and early.
    # Local endpoints (e.g. a local model server) may legitimately need no key.
    is_local = endpoint.startswith("http://127.0.0.1") or endpoint.startswith("http://localhost")
    if not is_local and not api_key:
        raise APIKeyError(f"No API key configured for {model_type} endpoint")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=VALIDATION_TIMEOUT) as client:
            content_blocks: list[dict[str, Any]] = [{"type": "input_text", "text": "ping"}]
            if is_vision:
                content_blocks.append(
                    {"type": "input_image", "image_url": _VALIDATION_IMAGE_DATA_URL}
                )
            # The probe body is Responses-API shaped (`input` + `input_text`).
            # The Responses API caps generation with `max_output_tokens`, NOT
            # `max_tokens` -- pick the param that matches the endpoint's wire
            # format. The budget is 64, NOT 1: the product default LLM is a
            # REASONING model ('programmer'), which spends tokens on hidden
            # reasoning before emitting any visible output. A budget of 1 leaves
            # zero room for output, so the provider returns failed / "No tokens
            # generated" for a perfectly healthy model (empirically: budget 1 ->
            # failed, budget 64 -> "pong"). 64 gives reasoning headroom for a
            # one-word reply; the truncation handling below is the backstop for
            # models that need even more.
            probe_token_budget = 64
            request_body: dict[str, Any] = {
                "model": model,
                "input": [{"role": "user", "content": content_blocks}],
            }
            if is_chat_completions_endpoint(endpoint):
                request_body["max_tokens"] = probe_token_budget
            else:
                request_body["max_output_tokens"] = probe_token_budget
            response = client.post(
                endpoint,
                headers=headers,
                json=request_body,
            )

            # 200 = model works
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    return True
                if payload.get("status") == "failed" or payload.get("error"):
                    error_payload = payload.get("error", {})
                    if isinstance(error_payload, dict):
                        error_message = str(error_payload.get("message", "")).strip()
                    else:
                        error_message = str(error_payload).strip()
                    # Reasoning models can spend the probe's deliberately small
                    # token budget entirely on hidden reasoning and emit zero
                    # output, so the provider reports failed / "No tokens
                    # generated" (output_tokens == 0). The model was reached and
                    # recognized -- that is OUR probe being too tight, NOT a broken
                    # model. Treat budget-truncation as available; do not warn.
                    lowered = error_message.lower()
                    truncation_markers = (
                        "no tokens generated",
                        "max_output_tokens",
                        "max output tokens",
                        "max_tokens",
                        "incomplete",
                    )
                    if any(marker in lowered for marker in truncation_markers):
                        return True
                    console.print(
                        "[yellow]  Warning: validation request returned failed "
                        f"status for {model}: {error_message or 'unknown provider error'}[/]"
                    )
                    # Honest, non-blocking: the probe genuinely failed, so do NOT
                    # claim a silent green check downstream. Pre-flight only -- the
                    # model may still work in the real pipeline, so we don't raise.
                    return False
                return True

            # 400 = bad request but model recognized
            if response.status_code == 400:
                return True

            # 401 = bad API key
            if response.status_code == 401:
                raise APIKeyError("Invalid API key")

            # 404 = model not found
            if response.status_code == 404:
                raise ModelValidationError(
                    f"{model_type} model '{model}' not found",
                    model_type=model_type,
                    model_name=model,
                )

            # 503 = service unavailable (might be model issue or server issue)
            if response.status_code == 503:
                # Try to parse error message
                try:
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", "")
                    if "model" in error_msg.lower():
                        raise ModelValidationError(
                            f"{model_type} model '{model}' unavailable: {error_msg}",
                            model_type=model_type,
                            model_name=model,
                        )
                except (ValueError, KeyError):
                    pass
                # Generic 503 - could be temporary. Unclear, not confirmed:
                # non-blocking but honest (no silent green check).
                console.print("[yellow]  Warning: API returned 503, model status unclear[/]")
                return False  # Optimistic - let pipeline try, but don't claim OK

            # Other errors - log but continue. Unclear status, not a confirmation.
            console.print(f"[yellow]  Warning: Unexpected status {response.status_code}[/]")
            return False

    except httpx.TimeoutException:
        console.print(f"[yellow]  Warning: Timeout checking {model_type} model[/]")
        return False  # Optimistic - let pipeline try, but don't claim OK

    except httpx.ConnectError as e:
        raise ModelValidationError(
            f"Cannot connect to API: {e}",
            model_type=model_type,
            model_name=model,
        ) from e

    except httpx.HTTPError as e:
        # Protocol/transport errors (e.g. an illegal header from a malformed key)
        # would otherwise escape as a raw traceback. Re-wrap as a friendly error.
        raise ModelValidationError(
            f"{model_type} endpoint check failed: {e}",
            model_type=model_type,
            model_name=model,
        ) from e


def _check_stt_model(config: ScreenScribeConfig) -> bool:
    """Check if STT endpoint is reachable.

    STT validation is limited - we can't easily test without audio.
    Just verify the endpoint responds.
    """
    # Don't send auth to localhost endpoints
    is_local = config.stt_endpoint.startswith("http://127.0.0.1") or config.stt_endpoint.startswith(
        "http://localhost"
    )

    # A remote endpoint with no key would send `Authorization: Bearer ` (empty),
    # which crashes deep in the HTTP stack (h11 rejects the empty value) as a raw
    # ~60-line traceback on a partial-key first run. Fail clearly and early.
    if not is_local and not config.get_stt_api_key():
        raise APIKeyError("No API key configured for STT endpoint")

    try:
        with httpx.Client(timeout=VALIDATION_TIMEOUT) as client:
            headers = {} if is_local else {"Authorization": f"Bearer {config.get_stt_api_key()}"}

            # POST with empty file to check endpoint responds (400 expected)
            response = client.post(
                config.stt_endpoint,
                headers=headers,
                data={"model": config.stt_model},
                files={"file": ("test.mp3", b"", "audio/mpeg")},
            )

            # 400 = endpoint works, just bad input (expected)
            if response.status_code == 400:
                return True

            # 401 = bad API key
            if response.status_code == 401:
                raise APIKeyError("Invalid API key for STT endpoint")

            # 200 would be weird with empty file, but OK
            if response.status_code == 200:
                return True

            # Other - optimistic
            return True

    except httpx.TimeoutException:
        console.print("[yellow]  Warning: Timeout checking STT endpoint[/]")
        return True

    except httpx.ConnectError as e:
        raise ModelValidationError(
            f"Cannot connect to STT API: {e}",
            model_type="STT",
            model_name=config.stt_model,
        ) from e

    except httpx.HTTPError as e:
        # Protocol/transport errors (e.g. an illegal header from a malformed key)
        # would otherwise escape as a raw traceback. Re-wrap as a friendly error.
        raise ModelValidationError(
            f"STT endpoint check failed: {e}",
            model_type="STT",
            model_name=config.stt_model,
        ) from e


def validate_models(
    config: ScreenScribeConfig,
    use_vision: bool = True,
    validate_stt: bool = True,
    validate_llm: bool = True,
) -> None:
    """Validate model availability before pipeline starts.

    Args:
        config: screenscribe configuration
        use_vision: Whether the unified VLM (visual/screenshot) analysis will run
        validate_stt: Whether to probe the STT endpoint. ``review --local`` runs
            transcription on a LOCAL Whisper server, so the cloud STT endpoint is
            never contacted and must not be probed -- but the LLM/Vision stages
            still hit the cloud and stay validated. ``analyze`` uses STT for voice
            notes, so it keeps this on.
        validate_llm: Whether to probe the LLM endpoint. ``analyze`` never calls
            the LLM (frame analysis is Vision-only), so it turns this off.

    Raises:
        APIKeyError: If API key is missing or invalid
        ModelValidationError: If a required model is not available
    """
    console.print("[dim]Validating configuration...[/]")

    # Check API key presence - at least one key must be configured
    has_any_key = (
        config.api_key or config.stt_api_key or config.llm_api_key or config.vision_api_key
    )
    if not has_any_key:
        raise APIKeyError(
            "No API key configured. "
            "Run `screenscribe config setup`, or set SCREENSCRIBE_API_KEY, "
            "or set per-endpoint keys such as SCREENSCRIBE_STT_API_KEY"
        )

    validation_results: list[tuple[str, str, bool]] = []

    # STT + LLM are BOTH core to a full review: transcription and the semantic
    # LLM pre-filter. --no-vision only skips the visual (VLM) stage, so it must
    # never silently degrade into a transcribe-only run with an empty report (the
    # prefilter would otherwise fail open downstream). The two flags gate the
    # probes per-stage so a run that legitimately does NOT hit one of these
    # endpoints (STT under --local, the LLM under analyze) is not probed for it.
    if validate_stt:
        try:
            stt_ok = _check_stt_model(config)
            validation_results.append(("STT", config.stt_model, stt_ok))
        except (APIKeyError, ModelValidationError):
            raise

    if validate_llm:
        try:
            llm_ok = _check_llm_model(config, config.llm_model, "LLM")
            validation_results.append(("LLM", config.llm_model, llm_ok))
        except (APIKeyError, ModelValidationError):
            raise

    # Vision model is only required when the visual (VLM) analysis will run.
    if use_vision:
        try:
            vision_ok = _check_llm_model(config, config.vision_model, "Vision")
            validation_results.append(("Vision", config.vision_model, vision_ok))
        except (APIKeyError, ModelValidationError):
            raise

    # Print results honestly: a green check ONLY for probes that actually
    # confirmed the model. A probe that ran but could not confirm (provider
    # reported a failure, 503, timeout, unexpected status) is reported as
    # unverified -- never a silent green check. This is non-blocking: pre-flight
    # cannot prove a model is broken, so the pipeline still proceeds.
    for model_type, model_name, ok in validation_results:
        if ok:
            console.print(f"  [green]\u2713[/] {model_type} model ({model_name})")
        else:
            console.print(
                f"  [yellow]?[/] {model_type} model ({model_name}) "
                "- could not verify, continuing anyway"
            )

    console.print()
