"""STT ``response_format`` negotiation for the file transcription path.

``transcribe_audio`` asks for ``verbose_json`` so Whisper-family servers return
per-segment timing and decode-confidence metadata. OpenAI's ``gpt-transcribe`` /
``gpt-4o-transcribe`` family rejects that value with HTTP 400
(``param=response_format``, ``code=unsupported_value``) and accepts only ``json``
or ``text``. Before this fix the whole review aborted at chunk 1/32 with
"Speech-to-text failed (HTTP 400)"; the fallback keeps the run alive with a
synthetic timeline and remembers the refusal per endpoint+model so a chunked run
does not repeat the rejected request for every chunk.

All STT calls are mocked — no real API is hit.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx
import pytest

from screenscribe.transcribe import transcribe_audio

ENDPOINT = "https://api.example.com/v1/audio/transcriptions"


def _status_error(url: str, body: dict[str, Any], status: int = 400) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _OkResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FormatSensitiveClient:
    """Fake httpx client that refuses ``verbose_json`` the way gpt-transcribe does."""

    def __init__(
        self,
        posted_formats: list[str],
        *,
        refuse_verbose_json: bool,
        refusal_body: dict[str, Any] | None = None,
    ) -> None:
        self._posted_formats = posted_formats
        self._refuse_verbose_json = refuse_verbose_json
        self._refusal_body = refusal_body or {
            "error": {
                "message": (
                    "response_format 'verbose_json' is not compatible with model "
                    "'gpt-transcribe-api-ev3'. Use 'json' or 'text' instead."
                ),
                "type": "invalid_request_error",
                "param": "response_format",
                "code": "unsupported_value",
            }
        }

    def __enter__(self) -> _FormatSensitiveClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def post(self, url: str, **kwargs: Any) -> _OkResponse:
        fmt = kwargs["data"]["response_format"]
        self._posted_formats.append(fmt)
        if fmt == "verbose_json" and self._refuse_verbose_json:
            raise _status_error(url, self._refusal_body)
        if fmt == "verbose_json":
            return _OkResponse(
                {
                    "text": "hello there",
                    "language": "en",
                    "segments": [
                        {"id": 0, "start": 0.0, "end": 1.5, "text": "hello there"},
                    ],
                }
            )
        return _OkResponse({"text": "hello there", "language": "en"})


@pytest.fixture
def audio_path(tmp_path: Path) -> Path:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"voice-data")
    return path


@pytest.fixture(autouse=True)
def _fresh_refusal_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-process memory of refused (endpoint, model) pairs must not leak
    # between tests.
    monkeypatch.setattr("screenscribe.transcribe._VERBOSE_JSON_REFUSED", set(), raising=False)


def _install(monkeypatch: pytest.MonkeyPatch, client: _FormatSensitiveClient) -> None:
    monkeypatch.setattr("screenscribe.transcribe.httpx.Client", lambda *a, **k: client)


def test_file_path_keeps_verbose_json_when_the_server_accepts_it(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    posted: list[str] = []
    _install(monkeypatch, _FormatSensitiveClient(posted, refuse_verbose_json=False))
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio(
        audio_path, language="en", stt_endpoint=ENDPOINT, stt_model="whisper-1", **auth_kwargs
    )

    assert posted == ["verbose_json"]
    assert result.timestamps_are_synthetic is False
    assert [s.end for s in result.segments] == [1.5]


def test_file_path_falls_back_to_json_when_the_server_rejects_verbose_json(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    posted: list[str] = []
    _install(monkeypatch, _FormatSensitiveClient(posted, refuse_verbose_json=True))
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio(
        audio_path, language="en", stt_endpoint=ENDPOINT, stt_model="vendor-stt-x", **auth_kwargs
    )

    # One rejected verbose_json request, then exactly one json retry.
    assert posted == ["verbose_json", "json"]
    assert result.text == "hello there"
    # ``json`` carries no segments: the timeline is synthetic and says so.
    assert result.timestamps_are_synthetic is True
    assert len(result.segments) == 1


def test_refusal_is_remembered_per_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    """A 32-chunk run must not pay one rejected request per chunk."""
    posted: list[str] = []
    _install(monkeypatch, _FormatSensitiveClient(posted, refuse_verbose_json=True))
    auth_kwargs = {"api" + "_key": "test-key"}

    for _ in range(3):
        transcribe_audio(
            audio_path,
            language="en",
            stt_endpoint=ENDPOINT,
            stt_model="vendor-stt-x",
            **auth_kwargs,
        )

    assert posted == ["verbose_json", "json", "json", "json"]

    # A different model on the same endpoint starts from verbose_json again.
    transcribe_audio(
        audio_path, language="en", stt_endpoint=ENDPOINT, stt_model="whisper-1", **auth_kwargs
    )
    assert posted[-2:] == ["verbose_json", "json"]


def test_unrelated_400_errors_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    posted: list[str] = []
    body = {
        "error": {
            "message": "Unsupported language 'xx'.",
            "type": "invalid_request_error",
            "param": "language",
            "code": "unsupported_value",
        }
    }
    _install(
        monkeypatch,
        _FormatSensitiveClient(posted, refuse_verbose_json=True, refusal_body=body),
    )
    auth_kwargs = {"api" + "_key": "test-key"}

    with pytest.raises(httpx.HTTPStatusError):
        transcribe_audio(
            audio_path, language="xx", stt_endpoint=ENDPOINT, stt_model="whisper-1", **auth_kwargs
        )

    # No fallback attempted: the server did not name response_format.
    assert posted == ["verbose_json"]


def test_flat_error_body_without_param_still_triggers_fallback(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path
) -> None:
    """Gateways that flatten the OpenAI error envelope only keep the message."""
    posted: list[str] = []
    body = {"message": "response_format 'verbose_json' is not supported. Use 'json' or 'text'."}
    _install(
        monkeypatch,
        _FormatSensitiveClient(posted, refuse_verbose_json=True, refusal_body=body),
    )
    auth_kwargs = {"api" + "_key": "test-key"}

    result = transcribe_audio(
        audio_path, language="en", stt_endpoint=ENDPOINT, stt_model="vendor-stt-x", **auth_kwargs
    )

    assert posted == ["verbose_json", "json"]
    assert result.text == "hello there"
