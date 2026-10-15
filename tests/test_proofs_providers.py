"""Tests for provider proof probes (network mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.proofs.providers import run_provider_probes
from screenscribe.proofs.receipts import VERDICT_NOT_PROVEN, VERDICT_PROVEN


class _MockSyncResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "mock error",
                request=None,
                response=self,  # type: ignore[arg-type]
            )

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class _MockAsyncStream:
    def __init__(self, status_code: int = 200, lines: list[str] | None = None) -> None:
        self.status_code = status_code
        self._lines = lines or []

    async def __aenter__(self) -> _MockAsyncStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def aread(self) -> bytes:
        return b"mock error body"

    async def aiter_lines(self) -> Any:
        for line in self._lines:
            yield line


class _MockAsyncClient:
    def __init__(self, stream: _MockAsyncStream) -> None:
        self._stream = stream

    async def __aenter__(self) -> _MockAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def stream(self, *args: object, **kwargs: object) -> _MockAsyncStream:
        return self._stream


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probes a test does not mock must fail locally, never reach a provider."""

    def refuse(*args: object, **kwargs: object) -> Any:
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


def _stt_payload() -> dict[str, Any]:
    return {
        "text": "proof",
        "segments": [{"id": 0, "start": 0.0, "end": 0.25, "text": "proof"}],
    }


def _unified_payload() -> dict[str, Any]:
    return {
        "id": "resp_proof",
        "output": [{"type": "message", "content": [{"type": "text", "text": "{}"}]}],
    }


def _ocr_payload() -> dict[str, Any]:
    return {"id": "resp_ocr", "text": "ok"}


def _responses_lines() -> list[str]:
    return [
        'data: {"type": "response.created", "response": {"id": "resp_agent"}}',
        'data: {"type": "response.output_text.delta", "delta": "pong"}',
        'data: {"type": "response.completed", "response": {"id": "resp_agent"}}',
        "data: [DONE]",
    ]


def test_provider_probes_stamped_when_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no API keys every provider receipt is NOT-PROVEN because the key is absent."""
    config = ScreenScribeConfig()
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    # Ensure anthropic key is absent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    receipts = run_provider_probes()
    assert len(receipts) == 6
    for receipt in receipts:
        assert receipt.verdict == VERDICT_NOT_PROVEN
        assert "absent" in receipt.key_state or receipt.element.endswith(":agent_anthropic")


def test_stt_openai_probe_proven_on_mocked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-compatible STT path stamps PROVEN when the mocked endpoint answers."""
    config = ScreenScribeConfig()
    config.stt_api_key = "sk-test"  # pragma: allowlist secret
    config.llm_api_key = "sk-test"  # pragma: allowlist secret
    config.vision_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    original_post = httpx.Client.post

    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> _MockSyncResponse:
        return _MockSyncResponse(_stt_payload())

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    receipts = run_provider_probes()
    stt = next(r for r in receipts if r.element == "screenscribe.transcribe:stt_openai_compat")
    assert stt.verdict == VERDICT_PROVEN
    assert stt.key_state == "present"
    monkeypatch.setattr(httpx.Client, "post", original_post)


def test_stt_openai_probe_not_proven_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-compatible STT path stamps NOT-PROVEN with the verbatim HTTP error."""
    config = ScreenScribeConfig()
    config.stt_api_key = "sk-test"  # pragma: allowlist secret
    config.llm_api_key = "sk-test"  # pragma: allowlist secret
    config.vision_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> _MockSyncResponse:
        return _MockSyncResponse({"error": {"message": "Payment Required"}}, status_code=402)

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    receipts = run_provider_probes()
    stt = next(r for r in receipts if r.element == "screenscribe.transcribe:stt_openai_compat")
    assert stt.verdict == VERDICT_NOT_PROVEN
    assert "402" in stt.reason or "Payment Required" in stt.reason


def test_unified_analysis_probe_proven_on_mocked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unified analysis path stamps PROVEN when the mocked LLM returns valid JSON."""
    config = ScreenScribeConfig()
    config.llm_api_key = "sk-test"  # pragma: allowlist secret
    config.vision_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> _MockSyncResponse:
        return _MockSyncResponse(_unified_payload())

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    receipts = run_provider_probes()
    unified = next(
        r for r in receipts if r.element == "screenscribe.unified.analyze_one:unified_analysis"
    )
    assert unified.verdict == VERDICT_PROVEN


def test_frame_ocr_probe_proven_on_mocked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame OCR path stamps PROVEN when the mocked vision endpoint answers."""
    config = ScreenScribeConfig()
    config.vision_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> _MockSyncResponse:
        return _MockSyncResponse(_ocr_payload())

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    receipts = run_provider_probes()
    ocr = next(r for r in receipts if r.element == "screenscribe.frame_ocr:frame_ocr")
    assert ocr.verdict == VERDICT_PROVEN


def test_agent_responses_probe_proven_on_mocked_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Responses path stamps PROVEN when the mocked SSE stream answers."""
    config = ScreenScribeConfig()
    config.llm_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    stream = _MockAsyncStream(lines=_responses_lines())
    fake_client = _MockAsyncClient(stream)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)

    receipts = run_provider_probes()
    agent = next(r for r in receipts if r.element == "screenscribe.agent.chat:agent_responses")
    assert agent.verdict == VERDICT_PROVEN


def test_agent_anthropic_probe_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Anthropic path stamps NOT-PROVEN when the fallback key is absent."""
    config = ScreenScribeConfig()
    config.llm_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    receipts = run_provider_probes()
    agent = next(r for r in receipts if r.element == "screenscribe.agent.chat:agent_anthropic")
    assert agent.verdict == VERDICT_NOT_PROVEN
    assert "Anthropic API key absent" in agent.reason


def test_xai_stt_probe_sends_nothing_when_stt_is_not_xai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With STT on LibraxisAI the xAI probe must not send the STT key to api.x.ai."""
    config = ScreenScribeConfig()
    config.stt_endpoint = "https://api.libraxis.cloud/v1/audio/transcriptions"
    config.stt_api_key = "lbx-test-key"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    sent: list[str] = []

    def record(self: httpx.Client, url: object, *args: object, **kwargs: object) -> Any:
        sent.append(str(url))
        return _MockSyncResponse(_stt_payload())

    monkeypatch.setattr(httpx.Client, "post", record)

    receipts = run_provider_probes()
    xai = next(r for r in receipts if r.element == "screenscribe.providers.xai:stt_xai")
    assert xai.verdict == VERDICT_NOT_PROVEN
    assert xai.reason == "STT routing does not target xAI"
    assert not [url for url in sent if "x.ai" in url]


def test_xai_stt_probe_targets_the_configured_xai_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With STT on xAI the probe calls the configured endpoint."""
    config = ScreenScribeConfig()
    config.stt_endpoint = "https://api.x.ai/v1/stt"
    config.stt_api_key = "xai-test-key"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    sent: list[str] = []

    def record(self: httpx.Client, url: object, *args: object, **kwargs: object) -> Any:
        sent.append(str(url))
        return _MockSyncResponse({"text": "proof", "words": []})

    monkeypatch.setattr(httpx.Client, "post", record)

    receipts = run_provider_probes()
    xai = next(r for r in receipts if r.element == "screenscribe.providers.xai:stt_xai")
    assert xai.request_essence == "POST https://api.x.ai/v1/stt"
    assert "https://api.x.ai/v1/stt" in sent


def test_stt_openai_probe_sends_nothing_when_stt_targets_xai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With STT routed to xAI the OpenAI-compat probe must not claim that path.

    transcribe_audio_bytes switches to the xAI transport for an xAI endpoint,
    so a PROVEN ``stt_openai_compat`` would be proven by somebody else's
    roundtrip. The xAI probe covers that endpoint; this one declines.
    """
    config = ScreenScribeConfig()
    config.stt_endpoint = "https://api.x.ai/v1/stt"
    config.stt_api_key = "xai-test-key"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)
    sent: list[str] = []

    def record(self: httpx.Client, url: object, *args: object, **kwargs: object) -> Any:
        sent.append(str(url))
        return _MockSyncResponse({"text": "proof", "words": []})

    monkeypatch.setattr(httpx.Client, "post", record)

    receipts = run_provider_probes()
    compat = next(r for r in receipts if r.element == "screenscribe.transcribe:stt_openai_compat")
    assert compat.verdict == VERDICT_NOT_PROVEN
    assert "xAI" in compat.reason
    # Only the dedicated xAI probe touched api.x.ai.
    assert [url for url in sent if "x.ai" in url] == ["https://api.x.ai/v1/stt"]


def test_agent_responses_probe_refuses_an_empty_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 stream with no text and no response id is not proof the model answered."""
    config = ScreenScribeConfig()
    config.llm_api_key = "sk-test"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    stream = _MockAsyncStream(lines=["data: [DONE]"])
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: _MockAsyncClient(stream))

    receipts = run_provider_probes()
    agent = next(r for r in receipts if r.element == "screenscribe.agent.chat:agent_responses")
    assert agent.verdict == VERDICT_NOT_PROVEN
    assert "empty response" in agent.reason


def test_receipts_never_carry_secrets_or_url_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider error bodies and endpoint queries are scrubbed before receipts.

    Receipts and MATRIX.md are on-disk diagnostics people share; a signed URL
    or a provider error echoing the key must not survive into them.
    """
    config = ScreenScribeConfig()
    config.stt_endpoint = (
        "https://stt.libraxis.cloud/v1/audio/transcriptions?token=tok-in-url"
    )
    config.stt_api_key = "sk-proof-secret-123"  # pragma: allowlist secret
    monkeypatch.setattr("screenscribe.proofs.providers.ScreenScribeConfig.load", lambda: config)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", raising=False)

    def fake_post(self: httpx.Client, *args: object, **kwargs: object) -> _MockSyncResponse:
        return _MockSyncResponse(
            {"error": {"message": "bad key sk-proof-secret-123"}}, status_code=401
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    receipts = run_provider_probes()
    stt = next(r for r in receipts if r.element == "screenscribe.transcribe:stt_openai_compat")
    assert stt.verdict == VERDICT_NOT_PROVEN
    joined = " ".join((stt.request_essence, stt.response_essence, stt.reason))
    assert "sk-proof-secret-123" not in joined  # pragma: allowlist secret
    assert "tok-in-url" not in joined
    assert "https://stt.libraxis.cloud/v1/audio/transcriptions" in stt.request_essence
