"""Tests for model validation (fail fast)."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.validation import (
    APIKeyError,
    ModelValidationError,
    _check_llm_model,
    _check_stt_model,
    validate_models,
)

# --- Fixtures ---


@pytest.fixture
def config() -> ScreenScribeConfig:
    """Basic config with API key."""
    cfg = ScreenScribeConfig()
    cfg.api_key = "test-api-key"  # pragma: allowlist secret
    cfg.stt_model = "whisper-1"
    cfg.llm_model = "ai-suggestions"
    cfg.vision_model = "ai-suggestions"
    return cfg


@pytest.fixture
def config_no_key() -> ScreenScribeConfig:
    """Config without API key."""
    cfg = ScreenScribeConfig()
    cfg.api_key = ""
    return cfg


# --- API Key Tests ---


class TestAPIKeyValidation:
    """Tests for API key presence validation."""

    def test_missing_api_key_raises_error(self, config_no_key: ScreenScribeConfig) -> None:
        """Missing API key should raise APIKeyError."""
        with pytest.raises(APIKeyError) as exc_info:
            validate_models(config_no_key, use_vision=True)
        assert "No API key configured" in str(exc_info.value)

    def test_api_key_present_passes(self, config: ScreenScribeConfig) -> None:
        """Present API key should not raise on key check."""
        # Mock HTTP responses to avoid actual API calls
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            # Should not raise
            validate_models(config, use_vision=False)


# --- LLM Model Tests ---


class TestLLMModelValidation:
    """Tests for LLM model availability check."""

    def test_model_available_200(self, config: ScreenScribeConfig) -> None:
        """200 response means model is available."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "completed"}
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.llm_model, "LLM")
            assert result is True

    def test_model_available_400(self, config: ScreenScribeConfig) -> None:
        """400 response means model exists (bad input but model recognized)."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.llm_model, "LLM")
            assert result is True

    def test_reasoning_model_truncation_is_not_a_failure(self, config: ScreenScribeConfig) -> None:
        """A reasoning model (e.g. the 'programmer' default) spends the probe's
        tiny token budget on reasoning and emits zero output, so the provider
        returns 200 with status=failed 'No tokens generated' and output_tokens=0.
        That is OUR probe being too tight, NOT a broken model -- the model was
        reached and recognized. It must be treated as available, never a probe
        failure. Regression guard: this is exactly the 'programmer' false-negative
        that surfaced as '? LLM model (programmer) - could not verify'."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "failed",
                "error": {
                    "message": (
                        "Failed to generate completion: Generation failed: No tokens generated"
                    )
                },
                "usage": {"input_tokens": 486, "output_tokens": 0, "total_tokens": 486},
            }
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.llm_model, "LLM")
            assert result is True

    def test_genuine_provider_failure_is_unverified(self, config: ScreenScribeConfig) -> None:
        """A real provider failure (not token-budget truncation) stays honest:
        the probe ran but did not confirm the model, so it returns False -- no
        silent green check. This protects the anti-fail-open contract while the
        reasoning-truncation case above is allowed through."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "failed",
                "error": {"message": "content_policy_violation: request rejected"},
            }
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.llm_model, "LLM")
            assert result is False

    def test_model_not_found_404(self, config: ScreenScribeConfig) -> None:
        """404 response means model not found."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            with pytest.raises(ModelValidationError) as exc_info:
                _check_llm_model(config, "nonexistent-model", "LLM")

            assert "nonexistent-model" in str(exc_info.value)
            assert exc_info.value.model_type == "LLM"
            assert exc_info.value.model_name == "nonexistent-model"

    def test_bad_api_key_401(self, config: ScreenScribeConfig) -> None:
        """401 response means bad API key."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            with pytest.raises(APIKeyError) as exc_info:
                _check_llm_model(config, config.llm_model, "LLM")

            assert "Invalid API key" in str(exc_info.value)

    def test_timeout_is_nonblocking_but_unverified(self, config: ScreenScribeConfig) -> None:
        """Timeout is non-blocking (no raise) but honest: unverified, not a green check."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.TimeoutException("timeout")
            )

            # Must not raise -- pre-flight cannot prove the model is broken.
            result = _check_llm_model(config, config.llm_model, "LLM")
            # But it did NOT confirm the model, so it must not claim success.
            assert result is False

    def test_connection_error_raises(self, config: ScreenScribeConfig) -> None:
        """Connection error should raise ModelValidationError."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError(
                "connection refused"
            )

            with pytest.raises(ModelValidationError) as exc_info:
                _check_llm_model(config, config.llm_model, "LLM")

            assert "Cannot connect" in str(exc_info.value)

    def test_llm_empty_key_raises_apikey_before_request(self) -> None:
        """A remote LLM/Vision endpoint with no key must fail before the wire too.

        Symmetric to the STT guard: an empty key would send 'Authorization:
        Bearer ' and crash deep in httpx/h11 as a raw traceback.
        """
        cfg = ScreenScribeConfig(
            llm_endpoint="https://api.example.com/v1/responses",
            vision_endpoint="https://api.example.com/v1/responses",
            llm_model="ai-suggestions",
            vision_model="ai-suggestions",
        )
        assert not cfg.get_llm_api_key()
        assert not cfg.get_vision_api_key()

        with patch("screenscribe.validation.httpx.Client") as mock_client:
            with pytest.raises(APIKeyError):
                _check_llm_model(cfg, cfg.llm_model, "LLM")
            with pytest.raises(APIKeyError):
                _check_llm_model(cfg, cfg.vision_model, "Vision")
            mock_client.assert_not_called()

    def test_responses_branch_uses_max_output_tokens(self, config: ScreenScribeConfig) -> None:
        """Responses-API endpoint must cap with max_output_tokens, not max_tokens.

        Regression P3-3/BH52: sending max_tokens to /v1/responses makes a healthy
        model emit "No tokens generated" / failed status -- a false probe failure.
        """
        config.llm_endpoint = "https://api.example.com/v1/responses"
        config.llm_api_key = "k"  # pragma: allowlist secret
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "completed"}
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = mock_response

            _check_llm_model(config, config.llm_model, "LLM")

            _, kwargs = client.post.call_args
            body = kwargs["json"]
            assert body["max_output_tokens"] == 64  # reasoning headroom, not 1
            assert "max_tokens" not in body

    def test_chat_completions_branch_uses_max_tokens(self, config: ScreenScribeConfig) -> None:
        """Chat Completions endpoint must keep max_tokens (its native param)."""
        config.llm_endpoint = "https://api.example.com/v1/chat/completions"
        config.llm_api_key = "k"  # pragma: allowlist secret
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "completed"}
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = mock_response

            _check_llm_model(config, config.llm_model, "LLM")

            _, kwargs = client.post.call_args
            body = kwargs["json"]
            assert body["max_tokens"] == 64  # reasoning headroom, not 1
            assert "max_output_tokens" not in body

    def test_working_model_returns_confirmed_true(self, config: ScreenScribeConfig) -> None:
        """A model that the provider confirms (status completed) is NOT blocked."""
        config.llm_endpoint = "https://api.example.com/v1/responses"
        config.llm_api_key = "k"  # pragma: allowlist secret
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "completed", "output_text": "pong"}
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.llm_model, "LLM")

            assert result is True

    def test_llm_protocol_error_is_wrapped(self, config: ScreenScribeConfig) -> None:
        """A protocol/transport error in the LLM check is wrapped, not a raw crash."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.LocalProtocolError("Illegal header value b'Bearer '")
            )

            with pytest.raises(ModelValidationError):
                _check_llm_model(config, config.llm_model, "LLM")


# --- STT Model Tests ---


class TestSTTModelValidation:
    """Tests for STT endpoint availability check."""

    def test_stt_endpoint_available_400(self, config: ScreenScribeConfig) -> None:
        """400 response means endpoint works (expected with empty file)."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_stt_model(config)
            assert result is True

    def test_stt_bad_api_key_401(self, config: ScreenScribeConfig) -> None:
        """401 response means bad API key for STT."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            with pytest.raises(APIKeyError):
                _check_stt_model(config)

    def test_stt_empty_key_raises_apikey_before_request(self) -> None:
        """A remote STT endpoint with no key must fail clearly, BEFORE the request.

        Regression: an empty key sent `Authorization: Bearer ` which crashed deep
        in httpx/h11 (LocalProtocolError) as a ~60-line raw traceback on a
        partial-key first run. Guard it instead of letting it reach the wire.
        """
        cfg = ScreenScribeConfig(
            stt_endpoint="https://api.example.com/v1/audio/transcriptions",
            stt_model="whisper-1",
        )
        assert not cfg.get_stt_api_key()

        with patch("screenscribe.validation.httpx.Client") as mock_client:
            with pytest.raises(APIKeyError):
                _check_stt_model(cfg)
            mock_client.assert_not_called()

    def test_stt_protocol_error_is_wrapped(self, config: ScreenScribeConfig) -> None:
        """A protocol/transport error becomes ModelValidationError, not a raw crash."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.LocalProtocolError("Illegal header value b'Bearer '")
            )

            with pytest.raises(ModelValidationError):
                _check_stt_model(config)


# --- Full Validation Tests ---


class TestValidateModels:
    """Tests for the main validate_models function."""

    def test_validates_stt_and_llm_when_no_vision(self, config: ScreenScribeConfig) -> None:
        """LLM is core: --no-vision still validates STT + LLM; only Vision is gated."""
        with patch("screenscribe.validation._check_stt_model") as mock_stt:
            with patch("screenscribe.validation._check_llm_model") as mock_llm:
                mock_stt.return_value = True
                mock_llm.return_value = True

                validate_models(config, use_vision=False)

                mock_stt.assert_called_once()
                mock_llm.assert_called_once()
                probed = {call.args[2] for call in mock_llm.call_args_list}
                assert probed == {"LLM"}

    def test_validates_llm_and_vision_when_ai(self, config: ScreenScribeConfig) -> None:
        """When use_vision=True, both LLM and Vision models are validated."""
        with patch("screenscribe.validation._check_stt_model") as mock_stt:
            with patch("screenscribe.validation._check_llm_model") as mock_llm:
                mock_stt.return_value = True
                mock_llm.return_value = True

                validate_models(config, use_vision=True)

                mock_stt.assert_called_once()
                assert mock_llm.call_count == 2
                probed = {call.args[2] for call in mock_llm.call_args_list}
                assert probed == {"LLM", "Vision"}

    def test_vision_validation_is_honest_but_nonblocking_for_failed_probe_body(
        self, config: ScreenScribeConfig
    ) -> None:
        """A genuinely-failed probe must NOT be reported as a silent success.

        Honest (returns False instead of a green-check True) yet non-blocking
        (does not raise) -- pre-flight cannot prove the model is broken.
        """
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "failed",
                "error": {"message": "Image features and image tokens do not match"},
            }
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.vision_model, "Vision")

            assert result is False

    def test_llm_validation_is_honest_but_nonblocking_for_failed_probe_body(
        self, config: ScreenScribeConfig
    ) -> None:
        """A genuinely-failed text-model probe must NOT report a silent success.

        Honest (returns False, no green check) yet non-blocking (no raise). The
        failure here is a REAL provider rejection (content policy), distinct from
        reasoning-token truncation -- which is treated as available (see
        test_reasoning_model_truncation_is_not_a_failure). This keeps the
        anti-fail-open contract while not mistaking a tight-budget reasoning model
        for a broken one.
        """
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "failed",
                "error": {"message": "content_policy_violation: request rejected"},
            }
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response

            result = _check_llm_model(config, config.llm_model, "LLM")

            assert result is False

    def test_vision_validation_sends_real_image_block(self, config: ScreenScribeConfig) -> None:
        """Vision validation must probe the image path, not just plain text."""
        with patch("screenscribe.validation.httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"status": "completed"}
            client = mock_client.return_value.__enter__.return_value
            client.post.return_value = mock_response

            result = _check_llm_model(config, config.vision_model, "Vision")

            assert result is True
            _, kwargs = client.post.call_args
            content = kwargs["json"]["input"][0]["content"]
            assert any(block.get("type") == "input_image" for block in content)

    def test_failing_llm_probe_is_reported_not_silently_checked(
        self, config: ScreenScribeConfig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A genuinely-failing LLM probe must NOT print a green check line.

        A real provider failure (here a content-policy rejection, NOT reasoning
        token truncation) must surface as an honest "could not verify" line while
        STILL continuing (non-blocking) -- never a misleading green check. Token
        truncation is the opposite case and is covered as available elsewhere.
        """
        with patch("screenscribe.validation._check_stt_model") as mock_stt:
            mock_stt.return_value = True
            with patch("screenscribe.validation.httpx.Client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "status": "failed",
                    "error": {"message": "content_policy_violation: request rejected"},
                }
                mock_client.return_value.__enter__.return_value.post.return_value = mock_response

                # Non-blocking: must not raise even though the probe failed.
                validate_models(config, use_vision=False)

        out = capsys.readouterr().out
        # No silent green check for the model that actually failed its probe.
        assert "✓ LLM model" not in out
        # Honest report of the unverified state.
        assert "could not verify" in out

    def test_working_models_still_print_green_check(
        self, config: ScreenScribeConfig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A confirmed model still gets its green check -- no over-correction."""
        with patch("screenscribe.validation._check_stt_model") as mock_stt:
            mock_stt.return_value = True
            with patch("screenscribe.validation.httpx.Client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"status": "completed"}
                mock_client.return_value.__enter__.return_value.post.return_value = mock_response

                validate_models(config, use_vision=False)

        out = capsys.readouterr().out
        assert "✓ LLM model" in out
        assert "could not verify" not in out

    def test_validates_all_when_full_pipeline(self, config: ScreenScribeConfig) -> None:
        """Full pipeline validates STT, LLM, and Vision."""
        with patch("screenscribe.validation._check_stt_model") as mock_stt:
            with patch("screenscribe.validation._check_llm_model") as mock_llm:
                mock_stt.return_value = True
                mock_llm.return_value = True

                validate_models(config, use_vision=True)

                mock_stt.assert_called_once()
                # LLM called twice: once for LLM, once for Vision
                assert mock_llm.call_count == 2
