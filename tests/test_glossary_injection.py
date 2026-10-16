"""Glossary consumption in pipeline re-runs (B-01).

When a review/analyze re-run has access to a corrections ledger
(``corrections.json`` in the output dir) or the global glossary, both the
VLM/LLM analysis payload and the OpenAI-compatible STT request carry a compact
glossary block. With no ledger, payloads stay byte-identical to the pre-B-01
shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, cast

import pytest

from screenscribe.agent import corrections
from screenscribe.transcribe import transcribe_audio_bytes
from screenscribe.unified.wire import _build_unified_payload

ENDPOINT = "https://api.example.com/v1/audio/transcriptions"


def _write_ledger(output_dir: Path, entries: list[dict[str, Any]]) -> None:
    """Write a valid corrections.json ledger."""
    ledger = {
        "version": 1,
        "report": output_dir.name,
        "entries": entries,
    }
    (output_dir / "corrections.json").write_text(json.dumps(ledger), encoding="utf-8")


_CORRECTION_ID = 0


def _correction(match: str, replacement: str, scope: str = "report") -> dict[str, Any]:
    global _CORRECTION_ID
    _CORRECTION_ID += 1
    return {
        "id": f"cor-{_CORRECTION_ID:08d}-1234-1234-1234-123456789abc",
        "kind": "transcript",
        "match": match,
        "replacement": replacement,
        "scope": scope,
        "source": "user",
        "note": "",
        "created_at": "2026-09-28T21:00:00Z",
    }


def _prompt_text(payload: dict[str, object]) -> str:
    """Extract the text prompt from a responses-shaped payload for assertions."""
    data = cast(dict[str, Any], payload)
    return str(data["input"][0]["content"][0]["text"])


# --- VLM/LLM payload injection -----------------------------------------------


def test_payload_injects_glossary_from_report_ledger(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    _write_ledger(
        output_dir,
        [
            _correction("cloud", "Claude"),
            _correction("chmura", "Claude"),
        ],
    )

    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="Analyze this finding.",
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
        output_dir=output_dir,
    )

    text = _prompt_text(payload)
    assert "Glossary of preferred transcriptions:" in text
    assert '"cloud" -> "Claude"' in text
    assert '"chmura" -> "Claude"' in text


def test_payload_is_unchanged_without_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ledger and no global glossary -> byte-identical prompt and payload shape."""

    # Ensure the global glossary lookup never returns entries during this test.
    # wire.py binds load_glossary at import time, so the patch must land on
    # the wire module's name, not on the corrections module (CodeRabbit QC).
    def _empty_glossary(glossary_dir: Path | None = None) -> corrections.LedgerLoad:
        return corrections.LedgerLoad(entries=[], warning=None, report="global")

    monkeypatch.setattr("screenscribe.unified.wire.load_glossary", _empty_glossary)

    prompt = "Analyze this finding."
    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt=prompt,
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
    )

    text = _prompt_text(payload)
    assert text == prompt
    assert "Glossary" not in text


def test_payload_escapes_glossary_literals(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    _write_ledger(
        output_dir,
        [_correction('say "hello"', 'say "hi"')],
    )

    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="Analyze.",
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
        output_dir=output_dir,
    )

    text = _prompt_text(payload)
    assert "Glossary of preferred transcriptions:" in text
    # JSON escaping keeps each entry on one literal line.
    assert '\\"hello\\"' in text
    assert '\\"hi\\"' in text
    assert text.count("\n- ") == 1


# --- STT request injection ---------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._recorder["url"] = url
        self._recorder["kwargs"] = kwargs
        return _FakeResponse(
            {
                "text": "Browser upload transcript",
                "language": "en",
                "response_id": "resp_upload_123",
            }
        )


def test_stt_request_injects_glossary_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    _write_ledger(output_dir, [_correction("cloud", "Claude")])
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        "screenscribe.transcribe.httpx.Client",
        lambda *args, **kwargs: _FakeClient(recorded),
    )

    transcribe_audio_bytes(
        b"voice-data",
        "recording.webm",
        content_type="audio/webm;codecs=opus",
        language="en",
        stt_endpoint=ENDPOINT,
        stt_model="test-model",
        api_key="test-key",  # pragma: allowlist secret
        output_dir=output_dir,
    )

    data = recorded["kwargs"]["data"]
    assert "prompt" in data
    assert "Glossary of preferred transcriptions:" in data["prompt"]
    assert '"cloud" -> "Claude"' in data["prompt"]
    assert data["response_format"] == "json"
    assert data["model"] == "test-model"


def test_stt_request_has_no_prompt_without_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _empty_glossary(glossary_dir: Path | None = None) -> corrections.LedgerLoad:
        return corrections.LedgerLoad(entries=[], warning=None, report="global")

    monkeypatch.setattr(corrections, "load_glossary", _empty_glossary)
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        "screenscribe.transcribe.httpx.Client",
        lambda *args, **kwargs: _FakeClient(recorded),
    )

    transcribe_audio_bytes(
        b"voice-data",
        "recording.webm",
        content_type="audio/webm;codecs=opus",
        language="en",
        stt_endpoint=ENDPOINT,
        stt_model="test-model",
        api_key="test-key",  # pragma: allowlist secret
    )

    data = recorded["kwargs"]["data"]
    assert "prompt" not in data
    assert data["response_format"] == "json"


# --- Size cap ----------------------------------------------------------------


def test_glossary_block_is_capped_at_50_entries(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    entries = [_correction(f"word{i}", f"term{i}") for i in range(60)]
    # Make timestamps monotonic so newest are unambiguous.
    for i, entry in enumerate(entries):
        entry["created_at"] = f"2026-09-28T21:{i:02d}:00Z"
    _write_ledger(output_dir, entries)

    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="Analyze.",
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
        output_dir=output_dir,
    )

    text = _prompt_text(payload)
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 50
    # Newest entries kept.
    assert '"word59" -> "term59"' in text
    # Oldest entries dropped.
    assert '"word0" -> "term0"' not in text
    assert '"word9" -> "term9"' not in text


def test_glossary_block_is_capped_at_2kb(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    # A single huge replacement should push the block past the cap immediately.
    huge = "x" * 3000
    _write_ledger(output_dir, [_correction("short", huge)])

    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="Analyze.",
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
        output_dir=output_dir,
    )

    text = _prompt_text(payload)
    # The header alone is not emitted as a glossary block.
    assert "Glossary of preferred transcriptions:" not in text
    assert len(text.encode("utf-8")) < 2100


# --- Global glossary ---------------------------------------------------------


def test_global_glossary_is_injected_when_no_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    glossary_dir = tmp_path / "glossary"
    glossary_dir.mkdir(parents=True)
    ledger = {
        "version": 1,
        "report": "global",
        "entries": [_correction("cloud", "Claude", scope="global")],
    }
    (glossary_dir / "corrections_glossary.json").write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(corrections, "default_glossary_dir", lambda: glossary_dir)

    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="Analyze.",
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
    )

    text = _prompt_text(payload)
    assert '"cloud" -> "Claude"' in text


# --- xAI no-op ---------------------------------------------------------------


def test_xai_stt_path_does_not_force_prompt_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """xAI STT has no OpenAI ``prompt`` param; glossary injection is an honest no-op."""
    from screenscribe.transcribe_types import TranscriptionResult

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    _write_ledger(output_dir, [_correction("cloud", "Claude")])
    calls: list[dict[str, Any]] = []

    def _fake_transcribe_file_xai(*args: Any, **kwargs: Any) -> TranscriptionResult:
        calls.append(kwargs)
        return TranscriptionResult(text="ok", segments=[], language="en")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_file_xai", _fake_transcribe_file_xai)

    transcribe_audio_bytes(
        b"voice-data",
        "recording.webm",
        language="en",
        stt_endpoint="https://api.x.ai/v1/stt",
        api_key="test-xai-key",  # pragma: allowlist secret
        output_dir=output_dir,
    )

    assert len(calls) == 1
    assert "prompt" not in calls[0]


def test_one_oversized_entry_does_not_empty_the_glossary(tmp_path: Path) -> None:
    """R2-05: an oversized newest entry is skipped, not a hard stop.

    The byte-cap loop walks newest-first and used to ``break`` on the first
    entry that did not fit, dropping every older (and shorter) entry with it.
    """
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    short = _correction("zeto", "ZETO")
    short["created_at"] = "2026-09-28T21:00:00Z"
    huge = _correction("short", "x" * 3000)
    huge["created_at"] = "2026-09-28T22:00:00Z"  # newest first in the block
    _write_ledger(output_dir, [short, huge])

    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="Analyze.",
        screenshot_path=None,
        previous_response_id=None,
        stream=False,
        output_dir=output_dir,
    )

    text = _prompt_text(payload)
    assert "Glossary of preferred transcriptions:" in text
    assert '"zeto" -> "ZETO"' in text
    assert "x" * 100 not in text
