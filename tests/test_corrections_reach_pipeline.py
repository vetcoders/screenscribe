"""Report-scoped corrections reach the production STT and analysis calls.

The glossary helpers read ``corrections.json`` from ``output_dir``. These tests
pin that every production caller hands that directory on, so a correction saved
with ``scope="report"`` shapes the next review run instead of only the helper
tests.

Falsification: drop ``output_dir=`` from any of the forwarded calls and the
matching assertion sees ``None``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.transcribe_types import Segment


def _detection() -> Detection:
    segment = Segment(id=1, start=1.0, end=2.0, text="the button is broken")
    return Detection(
        segment=segment, category="bug", keywords_found=["broken"], context=segment.text
    )


def test_unified_analysis_builds_the_payload_with_the_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from screenscribe.unified import analyze_one

    seen: list[Any] = []

    def spy(**kwargs: Any) -> dict[str, object]:
        seen.append(kwargs.get("output_dir"))
        raise RuntimeError("stop after payload")

    monkeypatch.setattr(analyze_one, "_build_unified_payload", spy)
    config = ScreenScribeConfig()
    config.vision_api_key = "sk-test"  # pragma: allowlist secret
    config.llm_api_key = "sk-test"  # pragma: allowlist secret

    analyze_one.analyze_finding_unified(_detection(), None, config, output_dir=tmp_path)
    analyze_one.analyze_finding_unified_streaming(_detection(), None, config, output_dir=tmp_path)

    assert seen
    assert all(value == tmp_path for value in seen), seen


def test_batch_analysis_forwards_the_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from screenscribe.unified.orchestrator import analyze_all_findings_unified

    seen: list[Any] = []

    def fake(*args: Any, **kwargs: Any) -> None:
        seen.append(kwargs.get("output_dir"))
        return None

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified_streaming", fake)
    monkeypatch.setattr("screenscribe.unified.orchestrator.STAGGER_DELAY", 0)
    config = ScreenScribeConfig()
    config.vision_api_key = "sk-test"  # pragma: allowlist secret
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8\xff")

    analyze_all_findings_unified(
        [(_detection(), shot), (_detection(), shot)], config, output_dir=tmp_path
    )

    assert seen
    assert all(value == tmp_path for value in seen), seen


def test_cli_transcription_forwards_the_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import screenscribe.cli as cli
    from screenscribe.cli_messages import _transcribe_audio_or_exit

    seen: dict[str, Any] = {}

    def fake(audio_path: Path, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(cli, "transcribe_audio_chunked", fake)

    _transcribe_audio_or_exit(
        tmp_path / "a.mp3",
        language="en",
        use_local=False,
        api_key="k",  # pragma: allowlist secret
        stt_endpoint="https://example.test/v1/audio/transcriptions",
        stt_model="m",
        output_dir=tmp_path,
    )

    assert seen["output_dir"] == tmp_path
