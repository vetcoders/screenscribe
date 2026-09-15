"""Screenscribe agent tools against the 08.22.27 report fixture (no video)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from screenscribe.agent.tools import ReportToolbelt

FIXTURE = Path(__file__).parent / "fixtures" / "agent_report_2026-09-15.json"


def _toolbelt(repo_root: Path | None = None) -> ReportToolbelt:
    report: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ReportToolbelt(report, repo_root=repo_root)


def test_list_findings_returns_fixture_rows() -> None:
    result = _toolbelt().list_findings()
    assert result["count"] == 13
    ids = {row["id"] for row in result["findings"]}
    assert 0 in ids
    assert result["findings"][0]["summary"]


def test_list_findings_filter_high() -> None:
    result = _toolbelt().list_findings("high")
    assert result["count"] >= 1
    assert all("high" in json.dumps(row).lower() for row in result["findings"])


def test_get_transcript_window() -> None:
    result = _toolbelt().get_transcript(start=3.0, end=8.0)
    assert result["count"] >= 1
    assert any("czemu" in str(seg.get("text") or "").lower() for seg in result["segments"])


def test_seek_returns_ui_instruction() -> None:
    assert _toolbelt().seek(12.5) == {"action": "seek", "timestamp": 12.5}


def test_show_frame_by_finding_id() -> None:
    result = _toolbelt().show_frame(finding_id=0)
    assert result["action"] == "show_frame"
    assert result["finding_id"] == 0
    assert result["timestamp"] is not None


def test_show_frame_by_timestamp() -> None:
    result = _toolbelt().show_frame(timestamp=4.0)
    assert result["action"] == "show_frame"
    assert result["finding_id"] is not None


def test_get_report_summary() -> None:
    result = _toolbelt().get_report_summary()
    assert "Najważniejsze" in result["executive_summary"]
    assert result["finding_count"] == 13
    assert result["severity_breakdown"]


def test_open_repo_file_requires_repo_root() -> None:
    result = _toolbelt().open_repo_file("README.md")
    assert "error" in result


def test_open_repo_file_reads_inside_root(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello agent", encoding="utf-8")
    result = _toolbelt(repo_root=tmp_path).open_repo_file("note.txt")
    assert result["content"] == "hello agent"
    assert result["path"] == "note.txt"


def test_open_repo_file_rejects_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    result = _toolbelt(repo_root=tmp_path).open_repo_file("../secret.txt")
    assert result.get("error") == "path escapes the repo root"
