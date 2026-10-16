"""One minimal live roundtrip per outbound provider path."""

from __future__ import annotations

import asyncio
import io
import re
import time
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..agent.chat import AgentProvider, _dispatch_round
from ..config import ScreenScribeConfig
from ..detect import Detection
from ..frame_ocr import ocr_frame
from ..providers.xai import transcribe_file_xai
from ..transcribe import _is_xai_endpoint, transcribe_audio_bytes
from ..transcribe_types import Segment
from ..unified.analyze_one import analyze_finding_unified
from .receipts import VERDICT_NOT_PROVEN, VERDICT_PROVEN, ProofReceipt


@dataclass(frozen=True)
class _ProbeContext:
    """Shared context for provider probes (built once per run)."""

    config: ScreenScribeConfig
    tiny_wav: bytes
    tmp_dir: Path


def _make_tiny_wav() -> bytes:
    """Generate a minimal valid 0.25s silent mono PCM16 WAV."""
    buffer = io.BytesIO()
    sample_rate = 16000
    duration_seconds = 0.25
    nframes = int(sample_rate * duration_seconds)
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * nframes)
    return buffer.getvalue()


def _key_state(present: bool) -> str:
    return "present" if present else "absent"


def _safe_url(url: str) -> str:
    """Endpoint identity for receipts: scheme://host/path, no query/fragment.

    A query string can carry credentials (signed URLs, gateway tokens), and
    receipts land in on-disk diagnostics that people share.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


_URL_RE = re.compile(r"\b(?:https?|wss?)://\S+")


def _secret_values(config: ScreenScribeConfig) -> tuple[str, ...]:
    """Every configured credential value worth scrubbing from diagnostics."""
    import os

    values = {
        config.api_key,
        config.stt_api_key,
        config.llm_api_key,
        config.vision_api_key,
        config.tts_api_key,
        config.stt_fallback_api_key,
        os.environ.get("ANTHROPIC_API_KEY", ""),
        os.environ.get("SCREENSCRIBE_AGENT_FALLBACK_API_KEY", ""),
    }
    values.update(config._libraxis_key_values)
    return tuple(v for v in values if v)


def _scrub_receipt(receipt: ProofReceipt, secrets: tuple[str, ...]) -> ProofReceipt:
    """Drop URL queries and mask configured secrets in one receipt.

    Provider error bodies and endpoint URLs end up verbatim in receipt JSON
    and MATRIX.md; neither may carry a credential out of the process.
    """

    def clean(text: str) -> str:
        text = _URL_RE.sub(lambda m: _safe_url(m.group(0)), text)
        for value in secrets:
            if value in text:
                text = text.replace(value, "***")
        return text

    return replace(
        receipt,
        request_essence=clean(receipt.request_essence),
        response_essence=clean(receipt.response_essence),
        reason=clean(receipt.reason),
    )


def _run_stt_openai(ctx: _ProbeContext) -> ProofReceipt:
    """Probe the OpenAI-compatible STT path via ``screenscribe.transcribe``."""
    element = "screenscribe.transcribe:stt_openai_compat"
    if _is_xai_endpoint(ctx.config.stt_endpoint):
        # transcribe_audio_bytes would silently switch to the xAI transport
        # here, so a PROVEN stamp would claim the OpenAI-compatible path on
        # the strength of an xAI roundtrip (the stt_xai probe's job).
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence="POST /v1/audio/transcriptions (tiny wav)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="STT routing targets xAI (proven by the stt_xai probe)",
            key_state=_key_state(bool(ctx.config.get_stt_api_key())),
        )
    key = ctx.config.get_stt_api_key()
    if not key:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence="POST /v1/audio/transcriptions (tiny wav)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="STT API key absent",
            key_state=_key_state(bool(key)),
        )

    start = time.perf_counter()
    try:
        result = transcribe_audio_bytes(
            ctx.tiny_wav,
            "proof.wav",
            api_key=key,
            stt_endpoint=ctx.config.stt_endpoint,
            stt_model=ctx.config.stt_model,
            response_format="json",
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.stt_endpoint} model={ctx.config.stt_model}",
            response_essence=f"{len(result.segments)} segment(s), text_len={len(result.text)}",
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN,
            key_state="present",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            body = exc.response.text[:200]
            reason = f"HTTP {exc.response.status_code}: {body or reason}"
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.stt_endpoint} model={ctx.config.stt_model}",
            response_essence="request failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=reason,
            key_state="present",
        )


def _run_stt_xai(ctx: _ProbeContext) -> ProofReceipt:
    """Probe the xAI STT path via ``screenscribe.providers.xai``."""
    element = "screenscribe.providers.xai:stt_xai"
    if not _is_xai_endpoint(ctx.config.stt_endpoint):
        # The STT key belongs to the configured STT provider; it must never be
        # sent to xAI just because this probe exists.
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence="POST /v1/stt (tiny wav)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="STT routing does not target xAI",
            key_state=_key_state(bool(ctx.config.get_stt_api_key())),
        )
    key = ctx.config.get_stt_api_key()
    if not key:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence="POST /v1/stt (tiny wav)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="STT API key absent",
            key_state=_key_state(bool(key)),
        )

    start = time.perf_counter()
    try:
        result = transcribe_file_xai(
            ctx.tiny_wav,
            "proof.wav",
            api_key=key,
            language="en",
            endpoint=ctx.config.stt_endpoint,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.stt_endpoint}",
            response_essence=f"{len(result.segments)} segment(s), text_len={len(result.text)}",
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN,
            key_state="present",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            body = exc.response.text[:200]
            reason = f"HTTP {exc.response.status_code}: {body or reason}"
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.stt_endpoint}",
            response_essence="request failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=reason,
            key_state="present",
        )


def _build_ping_payload(protocol: str, model: str) -> dict[str, Any]:
    """Minimal Responses/Anthropic payload for a ping turn."""
    if protocol == "anthropic":
        return {
            "model": model,
            "instructions": "Reply with the word pong and nothing else.",
            "input": [{"role": "user", "content": "ping"}],
            "tools": [],
            "previous_response_id": None,
        }
    # Responses API
    return {
        "model": model,
        "input": [{"role": "user", "content": "ping"}],
        "tools": [],
        "stream": True,
        "instructions": "Reply with the word pong and nothing else.",
    }


def _run_agent_responses(ctx: _ProbeContext) -> ProofReceipt:
    """Probe the agent Responses API path via ``screenscribe.agent.chat``."""
    element = "screenscribe.agent.chat:agent_responses"
    key = ctx.config.get_llm_api_key()
    if not key:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.llm_endpoint} (responses ping)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="LLM API key absent",
            key_state=_key_state(bool(key)),
        )

    provider = AgentProvider(
        name="primary",
        protocol="responses",
        key=key,
        model=ctx.config.llm_model,
        url=ctx.config.llm_endpoint,
        trust="processor",
        slot="primary",
        host=ctx.config._endpoint_provider(ctx.config.llm_endpoint) or "unknown",
    )
    payload = _build_ping_payload("responses", ctx.config.llm_model)

    start = time.perf_counter()
    try:
        round_result = asyncio.run(_dispatch_round(provider, payload))
        duration_ms = int((time.perf_counter() - start) * 1000)
        if round_result.error:
            return ProofReceipt(
                element=element,
                kind="provider",
                request_essence=f"POST {ctx.config.llm_endpoint} (responses ping)",
                response_essence="provider error event",
                duration_ms=duration_ms,
                verdict=VERDICT_NOT_PROVEN,
                reason=round_result.error,
                key_state="present",
            )
        essence = (
            f"text_len={len(round_result.text)}, "
            f"response_id_present={bool(round_result.response_id)}"
        )
        if not round_result.text.strip() and not round_result.response_id:
            # A 200 with neither text nor a response id proves only that the
            # socket opened; a PROVEN stamp needs evidence the model answered.
            return ProofReceipt(
                element=element,
                kind="provider",
                request_essence=f"POST {ctx.config.llm_endpoint} (responses ping)",
                response_essence=essence,
                duration_ms=duration_ms,
                verdict=VERDICT_NOT_PROVEN,
                reason="empty response: no text and no response id",
                key_state="present",
            )
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.llm_endpoint} (responses ping)",
            response_essence=essence,
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN,
            key_state="present",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            body = exc.response.text[:200]
            reason = f"HTTP {exc.response.status_code}: {body or reason}"
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.llm_endpoint} (responses ping)",
            response_essence="request failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=reason,
            key_state="present",
        )


def _run_agent_anthropic(ctx: _ProbeContext) -> ProofReceipt:
    """Probe the agent Anthropic Messages path via ``screenscribe.agent.chat``."""
    element = "screenscribe.agent.chat:agent_anthropic"

    fallback_key = (
        __import__("os").environ.get("SCREENSCRIBE_AGENT_FALLBACK_API_KEY")
        or __import__("os").environ.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    if not fallback_key:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence="POST https://api.anthropic.com/v1/messages (anthropic ping)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="Anthropic API key absent (SCREENSCRIBE_AGENT_FALLBACK_API_KEY or ANTHROPIC_API_KEY)",
            key_state="absent",
        )

    try:
        import anthropic  # noqa: F401
    except ImportError as exc:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence="POST https://api.anthropic.com/v1/messages (anthropic ping)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason=f"{type(exc).__name__}: {exc}",
            key_state="present",
        )

    url = (
        __import__("os").environ.get("SCREENSCRIBE_AGENT_FALLBACK_URL")
        or "https://api.anthropic.com/v1/messages"
    ).strip()
    model = (
        __import__("os").environ.get("SCREENSCRIBE_AGENT_FALLBACK_MODEL") or "claude-opus-4-6"
    ).strip()

    provider = AgentProvider(
        name="fallback",
        protocol="anthropic",
        key=fallback_key,
        model=model,
        url=url,
        trust="external",
        slot="fallback",
        host="api.anthropic.com",
    )
    payload = _build_ping_payload("anthropic", model)

    start = time.perf_counter()
    try:
        round_result = asyncio.run(_dispatch_round(provider, payload))
        duration_ms = int((time.perf_counter() - start) * 1000)
        if round_result.error:
            return ProofReceipt(
                element=element,
                kind="provider",
                request_essence=f"POST {url} (anthropic ping)",
                response_essence="provider error event",
                duration_ms=duration_ms,
                verdict=VERDICT_NOT_PROVEN,
                reason=round_result.error,
                key_state="present",
            )
        essence = (
            f"text_len={len(round_result.text)}, "
            f"response_id_present={bool(round_result.response_id)}"
        )
        if not round_result.text.strip() and not round_result.response_id:
            return ProofReceipt(
                element=element,
                kind="provider",
                request_essence=f"POST {url} (anthropic ping)",
                response_essence=essence,
                duration_ms=duration_ms,
                verdict=VERDICT_NOT_PROVEN,
                reason="empty response: no text and no response id",
                key_state="present",
            )
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {url} (anthropic ping)",
            response_essence=essence,
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN,
            key_state="present",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            body = exc.response.text[:200]
            reason = f"HTTP {exc.response.status_code}: {body or reason}"
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {url} (anthropic ping)",
            response_essence="request failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=reason,
            key_state="present",
        )


def _run_unified_analysis(ctx: _ProbeContext) -> ProofReceipt:
    """Probe the unified VLM/LLM analysis path via ``screenscribe.unified.analyze_one``."""
    element = "screenscribe.unified.analyze_one:unified_analysis"
    key = ctx.config.get_llm_api_key()
    if not key:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.llm_endpoint} (text-only unified ping)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="LLM API key absent",
            key_state=_key_state(bool(key)),
        )

    detection = Detection(
        segment=Segment(id=0, start=0.0, end=1.0, text="proof ping"),
        category="bug",
        keywords_found=[],
        context="minimal proof ping",
    )

    start = time.perf_counter()
    try:
        finding = analyze_finding_unified(
            detection=detection,
            screenshot_path=None,
            config=ctx.config,
            previous_response_id=None,
            force_text_only=True,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if finding is None:
            return ProofReceipt(
                element=element,
                kind="provider",
                request_essence=f"POST {ctx.config.llm_endpoint} (text-only unified ping)",
                response_essence="no finding returned",
                duration_ms=duration_ms,
                verdict=VERDICT_NOT_PROVEN,
                reason="analyze_finding_unified returned None (empty/unparseable response)",
                key_state="present",
            )
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.llm_endpoint} (text-only unified ping)",
            response_essence=f"response_id_present={bool(getattr(finding, 'response_id', ''))}",
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN,
            key_state="present",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            body = exc.response.text[:200]
            reason = f"HTTP {exc.response.status_code}: {body or reason}"
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.llm_endpoint} (text-only unified ping)",
            response_essence="request failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=reason,
            key_state="present",
        )


def _run_frame_ocr(ctx: _ProbeContext) -> ProofReceipt:
    """Probe the frame OCR path via ``screenscribe.frame_ocr``."""
    element = "screenscribe.frame_ocr:frame_ocr"
    key = ctx.config.get_vision_api_key()
    if not key:
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.vision_endpoint} (OCR ping)",
            response_essence="no request sent",
            duration_ms=0,
            verdict=VERDICT_NOT_PROVEN,
            reason="Vision API key absent",
            key_state=_key_state(bool(key)),
        )

    # Create a tiny valid PNG for the vision endpoint. Two field findings are
    # baked in: the original literal had corrupt CRCs (providers rejected it as
    # "Invalid PNG image"), and xAI requires >= 512 total pixels, so the frame
    # is 32x32 (1024 px), zlib-verified.
    frame_path = ctx.tmp_dir / "proof_frame.png"
    frame_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452"
            "00000020000000200802000000fc18eda3"
            "0000002a49444154789c633821274753c4"  # pragma: allowlist secret
            "306ac1a805a3168c5a306ac1a805a3168c"
            "5a306ac1a805a3160c150b0025cb103dcd"  # pragma: allowlist secret
            "4a1e0d0000000049454e44ae426082"
        )
    )

    start = time.perf_counter()
    try:
        # A fresh cache dir per run: a hit in the user's OCR cache would
        # stamp PROVEN without any live roundtrip, which is the opposite
        # of what this probe exists to prove.
        text, response_id = ocr_frame(frame_path, ctx.config, cache_dir=ctx.tmp_dir / "ocr-cache")
        duration_ms = int((time.perf_counter() - start) * 1000)
        proven = bool(text.strip() or response_id)
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.vision_endpoint} (OCR ping)",
            response_essence=f"text_len={len(text)}, response_id_present={bool(response_id)}",
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN if proven else VERDICT_NOT_PROVEN,
            reason="" if proven else "empty response: no text and no response id",
            key_state="present",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        reason = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            body = exc.response.text[:200]
            reason = f"HTTP {exc.response.status_code}: {body or reason}"
        return ProofReceipt(
            element=element,
            kind="provider",
            request_essence=f"POST {ctx.config.vision_endpoint} (OCR ping)",
            response_essence="request failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=reason,
            key_state="present",
        )


def run_provider_probes() -> list[ProofReceipt]:
    """Run one minimal roundtrip per inventoried provider path."""
    import tempfile

    config = ScreenScribeConfig.load()
    tmp_dir = Path(tempfile.mkdtemp(prefix="screenscribe-proof-providers-"))
    try:
        ctx = _ProbeContext(config=config, tiny_wav=_make_tiny_wav(), tmp_dir=tmp_dir)
        secrets = _secret_values(config)
        return [
            _scrub_receipt(receipt, secrets)
            for receipt in (
                _run_stt_openai(ctx),
                _run_stt_xai(ctx),
                _run_agent_responses(ctx),
                _run_agent_anthropic(ctx),
                _run_unified_analysis(ctx),
                _run_frame_ocr(ctx),
            )
        ]
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
