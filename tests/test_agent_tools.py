"""Screenscribe agent tools against the 08.22.27 report fixture (no video)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from screenscribe.agent.tools import ReportToolbelt, responses_tool_schemas

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


def test_list_findings_filter_includes_raw_text_and_context() -> None:
    report = {
        "findings": [
            {
                "id": 1,
                "text": "literal-user-wording",
                "context": "later-correction",
                "unified_analysis": {"summary": "different model summary"},
            }
        ]
    }
    belt = ReportToolbelt(report)
    assert belt.list_findings("literal-user-wording")["count"] == 1
    assert belt.list_findings("later-correction")["count"] == 1


def test_get_transcript_window() -> None:
    result = _toolbelt().get_transcript(start=3.0, end=8.0)
    assert result["count"] >= 1
    assert any("czemu" in str(seg.get("text") or "").lower() for seg in result["segments"])


def test_seek_returns_ui_instruction() -> None:
    assert _toolbelt().seek(12.5) == {"action": "seek", "timestamp": 12.5}


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf"), float("-inf"), "nan"])
def test_timestamp_tools_reject_nonfinite_or_negative(timestamp: object) -> None:
    belt = _toolbelt()
    assert "finite, non-negative" in belt.seek(timestamp)["error"]
    assert "finite, non-negative" in belt.add_finding(timestamp, "summary", "high", "bug")["error"]
    assert "finite, non-negative" in belt.get_transcript(start=timestamp)["error"]


def test_transcript_window_rejects_reversed_range() -> None:
    assert _toolbelt().get_transcript(start=5, end=4) == {
        "error": "start must be less than or equal to end"
    }


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


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".git/config",
        "credentials.json",
        "private.key",
        "nested/api_token.txt",
        "id_ed25519",
    ],
)
def test_open_repo_file_rejects_sensitive_paths(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-real-secret", encoding="utf-8")
    result = _toolbelt(repo_root=tmp_path).open_repo_file(relative)
    assert result == {"error": "path is not an allowlisted source or documentation file"}


def test_open_repo_file_rejects_non_source_extension(tmp_path: Path) -> None:
    path = tmp_path / "archive.bin"
    path.write_bytes(b"binary")
    result = _toolbelt(repo_root=tmp_path).open_repo_file(path.name)
    assert result == {"error": "path is not an allowlisted source or documentation file"}


def test_repo_tool_schema_does_not_claim_nonexistent_cli_flag() -> None:
    schema = next(
        tool
        for tool in responses_tool_schemas(include_repo=True)
        if tool["name"] == "open_repo_file"
    )
    assert "--repo" not in schema["description"]
    assert "programmatically" in schema["description"]
