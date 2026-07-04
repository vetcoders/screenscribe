"""Streaming unified analysis regression tests."""

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Literal

import pytest
from rich.console import Console

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import analyze_finding_unified, analyze_finding_unified_streaming


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self) -> "_FakeStreamResponse":
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


class _FakeJsonResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.text = json.dumps(payload)
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, *args: Any, lines: list[str] | None = None, **kwargs: Any) -> None:
        self._lines = lines
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
        final_text = "Not JSON but still useful fallback summary."
        response_payload = {
            "id": "resp_test_123",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": final_text}],
                }
            ],
        }
        lines = self._lines or [
            "event: response.created",
            f"data: {json.dumps({'type': 'response.created', 'response': {'id': 'resp_test_123'}})}",
            f"data: {json.dumps({'type': 'response.output_text.done', 'text': final_text})}",
            f"data: {json.dumps({'type': 'response.completed', 'response': response_payload})}",
            "data: [DONE]",
        ]
        return _FakeStreamResponse(lines)

    def post(self, *args: Any, **kwargs: Any) -> _FakeJsonResponse:
        payload = {
            "id": "resp_nonstream_fallback",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"is_issue": true, "severity": "medium", "summary": "Fallback succeeded."}',
                        }
                    ],
                }
            ],
        }
        return _FakeJsonResponse(payload)


class _ImageErrorThenTextOnlyClient:
    payloads: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_ImageErrorThenTextOnlyClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
        payload = kwargs["json"]
        _ImageErrorThenTextOnlyClient.payloads.append(payload)
        content_items = payload["input"][0]["content"]
        has_image = any(item.get("type") == "input_image" for item in content_items)
        if has_image:
            return _FakeStreamResponse(
                [
                    "event: response.created",
                    "data: "
                    + json.dumps(
                        {"type": "response.created", "response": {"id": "resp_image_failed"}}
                    ),
                    "event: error",
                    "data: "
                    + json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "message": "Image features and image tokens do not match",
                            },
                        }
                    ),
                    "data: [DONE]",
                ]
            )

        return _FakeStreamResponse(
            [
                "event: response.created",
                "data: "
                + json.dumps(
                    {"type": "response.created", "response": {"id": "resp_text_only_success"}}
                ),
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_text.done",
                        "text": '{"is_issue": true, "severity": "high", "summary": "Text-only fallback succeeded."}',
                    }
                ),
                "data: [DONE]",
            ]
        )

    def post(self, *args: Any, **kwargs: Any) -> _FakeJsonResponse:
        payload = kwargs["json"]
        _ImageErrorThenTextOnlyClient.payloads.append(payload)
        content_items = payload["input"][0]["content"]
        has_image = any(item.get("type") == "input_image" for item in content_items)
        if has_image:
            return _FakeJsonResponse(
                {
                    "id": "resp_nonstream_failed",
                    "status": "failed",
                    "output": [],
                    "error": {"message": "Image features and image tokens do not match"},
                }
            )
        return _FakeJsonResponse(
            {
                "id": "resp_nonstream_text_only_success",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"is_issue": true, "severity": "medium", "summary": "Non-streaming text-only fallback succeeded."}',
                            }
                        ],
                    }
                ],
            }
        )


class _NonJsonResponseClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_NonJsonResponseClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
        return _FakeStreamResponse(
            [
                "event: response.created",
                "data: "
                + json.dumps({"type": "response.created", "response": {"id": "resp_stream_text"}}),
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_text.done",
                        "text": "Not JSON but still useful fallback summary.",
                    }
                ),
                "data: [DONE]",
            ]
        )

    def post(self, *args: Any, **kwargs: Any) -> _FakeJsonResponse:
        return _FakeJsonResponse(
            {
                "id": "resp_nonstream_text",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Still not JSON, but here is a human summary.",
                            }
                        ],
                    }
                ],
            }
        )


def test_streaming_unified_analysis_keeps_final_text_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    detection = Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )

    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _NonJsonResponseClient)

    result = analyze_finding_unified_streaming(detection, screenshot, config)

    assert result is not None
    assert result.response_id == "resp_stream_text"
    assert result.summary == "Not JSON but still useful fallback summary."
    assert result.confidence == "degraded"
    assert result.parsed_from_unstructured_output is True
    assert result.is_issue is False
    assert result.severity == "none"
    assert result.suggested_fix


def test_streaming_output_text_done_replaces_delta_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    detection = Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )

    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    lines = [
        "event: response.created",
        "data: " + json.dumps({"type": "response.created", "response": {"id": "resp_test_stream"}}),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": "Hel"}),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": "lo"}),
        "data: " + json.dumps({"type": "response.output_text.done", "text": "Hello"}),
        "data: [DONE]",
    ]
    # Ensure only `Hello` lands in collected content, not `HelHelloLo...` or duplicates.
    monkeypatch.setattr(
        "screenscribe.unified_analysis.httpx.Client",
        lambda *args, **kwargs: _FakeClient(*args, lines=lines, **kwargs),
    )

    result = analyze_finding_unified_streaming(detection, screenshot, config)

    assert result is not None
    assert result.summary == "Hello"


def test_streaming_error_event_falls_back_to_non_streaming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    detection = Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )

    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    lines = [
        "event: response.created",
        "data: " + json.dumps({"type": "response.created", "response": {"id": "resp_test_stream"}}),
        "event: error",
        "data: "
        + json.dumps(
            {"type": "error", "error": {"message": "Image features and image tokens do not match"}}
        ),
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "screenscribe.unified_analysis.httpx.Client",
        lambda *args, **kwargs: _FakeClient(*args, lines=lines, **kwargs),
    )

    result = analyze_finding_unified_streaming(detection, screenshot, config)

    assert result is not None
    assert result.response_id == "resp_nonstream_fallback"
    assert result.summary == "Fallback succeeded."


def test_streaming_image_failure_falls_back_to_text_only_streaming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")
    detection = Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )
    config = ScreenScribeConfig(
        llm_api_key="llm-test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="llm-test-model",
        vision_api_key="vision-test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="vision-test-model",
    )

    _ImageErrorThenTextOnlyClient.payloads = []
    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _ImageErrorThenTextOnlyClient)

    result = analyze_finding_unified_streaming(detection, screenshot, config)

    assert result is not None
    assert result.response_id == "resp_text_only_success"
    assert result.summary == "Text-only fallback succeeded."
    assert len(_ImageErrorThenTextOnlyClient.payloads) == 2
    assert any(
        item.get("type") == "input_image"
        for item in _ImageErrorThenTextOnlyClient.payloads[0]["input"][0]["content"]
    )
    assert all(
        item.get("type") != "input_image"
        for item in _ImageErrorThenTextOnlyClient.payloads[1]["input"][0]["content"]
    )


def test_non_streaming_failed_image_body_falls_back_to_text_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")
    detection = Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )
    config = ScreenScribeConfig(
        llm_api_key="llm-test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="llm-test-model",
        vision_api_key="vision-test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="vision-test-model",
    )

    _ImageErrorThenTextOnlyClient.payloads = []
    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _ImageErrorThenTextOnlyClient)

    result = analyze_finding_unified(detection, screenshot, config)

    assert result is not None
    assert result.response_id == "resp_nonstream_text_only_success"
    assert result.summary == "Non-streaming text-only fallback succeeded."
    assert len(_ImageErrorThenTextOnlyClient.payloads) == 2


def test_non_streaming_unified_analysis_marks_non_json_output_as_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")

    detection = Detection(
        segment=Segment(id=1, start=12.5, end=15.0, text="Przycisk dalej nie działa poprawnie."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )

    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _NonJsonResponseClient)

    result = analyze_finding_unified(detection, screenshot, config, force_text_only=True)

    assert result is not None
    assert result.response_id == "resp_nonstream_text"
    assert result.summary == "Still not JSON, but here is a human summary."
    assert result.confidence == "degraded"
    assert result.parsed_from_unstructured_output is True
    assert result.is_issue is False
    assert result.severity == "none"


# --- A3: analyze no-key guard (verify-sweep Hole B) --------------------------
#
# analyze_finding_unified_streaming returned a SILENT None when the relevant API
# key was missing. The orchestrator guards this upstream and the text-only
# fallback recursion relies on None as a "skip" signal, so the function still
# returns None -- but a *direct* caller would get a bare None indistinguishable
# from a genuine "no finding". The no-key case must now be LOUD (warning), not a
# quiet fail-open.


def test_analyze_one_no_key_warns_instead_of_silent_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct no-key call warns loudly and still returns None (contract kept)."""
    detection = Detection(
        segment=Segment(id=1, start=1.0, end=2.0, text="The button does nothing."),
        category="bug",
        keywords_found=["semantic:bug"],
        context="User reports a broken button.",
    )
    # No key on any field -> get_llm_api_key()/get_vision_api_key() both "".
    config = ScreenScribeConfig(
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
    )
    assert config.get_llm_api_key() == ""  # precondition: no key at all

    recording = Console(record=True, width=100)
    monkeypatch.setattr("screenscribe.unified.analyze_one.console", recording)

    # screenshot_path=None -> text-only backend -> uses the (missing) LLM key.
    result = analyze_finding_unified_streaming(detection, None, config)
    output = " ".join(recording.export_text().split())

    assert result is None  # None preserved: orchestrator/recursion skip signal
    assert "API key" in output  # but no longer silent
    assert "skip" in output.lower()
