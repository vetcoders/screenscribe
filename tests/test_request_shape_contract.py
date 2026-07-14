"""Request-shape contract tests for the STT/LLM/VLM outgoing HTTP calls.

These pin the CLIENT side of the wire contract (method, path, headers, body
shape) offline, so a drift in how *we* build a request is a red test here
instead of a surprise in the opt-in nightly `stt-contract.yml` workflow (which
only catches drift in the LIVE server, not in our own request construction).

Zero network: every test replaces ``httpx.Client`` with a fake that either
records the call kwargs directly, or builds a real (unsent) ``httpx.Request``
from them so header/body encoding (e.g. multipart boundary) can be asserted
precisely. All config values are fictional test fixtures -- no real secrets,
no real endpoints.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Literal

import httpx
import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.keywords import KeywordsConfig
from screenscribe.semantic_filter import semantic_prefilter
from screenscribe.transcribe import Segment, TranscriptionResult, transcribe_audio_bytes
from screenscribe.unified.analyze_one import analyze_finding_unified_streaming

# --- 1. STT: multipart upload contract --------------------------------------


class _FakeSTTResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _CapturingSTTClient:
    """httpx.Client stand-in that builds a real (unsent) Request to inspect
    the actual multipart encoding httpx would produce, without hitting the
    network."""

    def __init__(self, recorder: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self._recorder = recorder

    def __enter__(self) -> _CapturingSTTClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeSTTResponse:
        request = httpx.Request("POST", url, **kwargs)
        self._recorder["request"] = request
        self._recorder["kwargs"] = kwargs
        return _FakeSTTResponse(
            {"text": "shape contract stub", "language": "en", "response_id": "resp_contract"}
        )


def test_stt_upload_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """STT multipart POST: method+path, Bearer auth, multipart body, file+model fields."""
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        "screenscribe.transcribe.httpx.Client",
        lambda *args, **kwargs: _CapturingSTTClient(recorded, *args, **kwargs),
    )

    transcribe_audio_bytes(
        b"fake-audio-bytes",
        "note.webm",
        content_type="audio/webm;codecs=opus",
        language="en",
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="fictional-stt-model",
        api_key="fictional-stt-key",  # pragma: allowlist secret
    )

    request: httpx.Request = recorded["request"]

    # Method + path.
    assert request.method == "POST"
    assert str(request.url) == "https://api.example.com/v1/audio/transcriptions"

    # Bearer auth header.
    assert request.headers["authorization"] == "Bearer fictional-stt-key"

    # Multipart body (not JSON/urlencoded).
    assert request.headers["content-type"].startswith("multipart/form-data")

    # File field name + model field, at the kwargs level (pre-encoding).
    kwargs = recorded["kwargs"]
    assert kwargs["files"]["file"][0] == "note.webm"
    assert kwargs["data"]["model"] == "fictional-stt-model"


# --- 2. LLM (semantic prefilter): chat-completions contract -----------------


class _CapturingStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self) -> _CapturingStreamResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines


class _CapturingStreamClient:
    """Captures method/url/headers/json body of the streaming POST call."""

    captured: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _CapturingStreamClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def stream(self, method: str, url: str, **kwargs: Any) -> _CapturingStreamResponse:
        _CapturingStreamClient.captured.append({"method": method, "url": url, "kwargs": kwargs})
        # Terminate immediately -- request-shape tests only need the captured
        # call, not a parsed result.
        return _CapturingStreamResponse(["data: [DONE]"])


def _sample_transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="Przycisk nie działa.",
        segments=[Segment(id=0, start=0.0, end=3.0, text="Przycisk nie działa.")],
        language="pl",
    )


def test_llm_prefilter_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Semantic pre-filter POST on a chat/completions endpoint: path, model,
    messages[role/content] shape, stream flag matching the call site."""
    _CapturingStreamClient.captured = []
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _CapturingStreamClient)

    config = ScreenScribeConfig(
        llm_api_key="fictional-llm-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/chat/completions",
        llm_model="fictional-llm-model",
        language="en",
    )

    semantic_prefilter(
        _sample_transcription(),
        config,
        keywords=KeywordsConfig(),
    )

    assert _CapturingStreamClient.captured, "semantic_prefilter did not call httpx stream POST"
    call = _CapturingStreamClient.captured[0]

    # Method + path (chat-completions, not the Responses API tree).
    assert call["method"] == "POST"
    assert call["url"] == "https://api.example.com/v1/chat/completions"

    # Bearer auth header.
    assert call["kwargs"]["headers"]["Authorization"] == "Bearer fictional-llm-key"

    body = call["kwargs"]["json"]

    # Model from config.
    assert body["model"] == "fictional-llm-model"

    # Chat Completions messages[role/content] shape (no image -> content is a
    # plain string, not a content-part list).
    assert len(body["messages"]) == 1
    message = body["messages"][0]
    assert set(message.keys()) == {"role", "content"}
    assert message["role"] == "user"
    assert isinstance(message["content"], str)
    assert "Przycisk nie działa." in message["content"]

    # semantic_prefilter always streams -- the flag must reflect that call site.
    assert body["stream"] is True


# --- 3. VLM (unified analyze): chat-completions + image contract ------------


def _detection() -> Detection:
    return Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )


def test_vlm_unified_analyze_request_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unified VLM analyze POST on a chat/completions endpoint: path, model,
    messages[role/content] shape with an image_url part, stream flag."""
    _CapturingStreamClient.captured = []
    monkeypatch.setattr("screenscribe.unified.analyze_one.httpx.Client", _CapturingStreamClient)

    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"fake-png-bytes")

    config = ScreenScribeConfig(
        # No llm_api_key/api_key configured: if the (unrelated) text-only
        # fallback path were ever reached it short-circuits on the missing
        # key instead of issuing a second, confusing captured call.
        vision_api_key="fictional-vision-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/chat/completions",
        vision_model="fictional-vision-model",
        language="en",
    )

    analyze_finding_unified_streaming(_detection(), screenshot, config)

    assert _CapturingStreamClient.captured, "unified analyze did not call httpx stream POST"
    call = _CapturingStreamClient.captured[0]

    # Method + path (chat-completions, not the Responses API tree).
    assert call["method"] == "POST"
    assert call["url"] == "https://api.example.com/v1/chat/completions"

    # Bearer auth header (vision key, since a screenshot is present).
    assert call["kwargs"]["headers"]["Authorization"] == "Bearer fictional-vision-key"

    body = call["kwargs"]["json"]

    # Model from config (vision model, screenshot-backed call).
    assert body["model"] == "fictional-vision-model"

    # Chat Completions messages[role/content] shape, image part included.
    assert body["messages"][0]["role"] == "user"
    content_parts = body["messages"][0]["content"]
    assert isinstance(content_parts, list)
    part_types = [part["type"] for part in content_parts]
    assert "text" in part_types
    image_parts = [part for part in content_parts if part["type"] == "image_url"]
    assert len(image_parts) == 1
    image_url = image_parts[0]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")

    # This is the streaming call site -- stream must be set.
    assert body["stream"] is True
