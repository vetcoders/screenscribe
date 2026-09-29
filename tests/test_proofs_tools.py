"""Tests for the tool proof harness (local, no network)."""

from __future__ import annotations

from pathlib import Path

from screenscribe.agent.tools import TOOL_NAMES
from screenscribe.proofs.receipts import VERDICT_PROVEN
from screenscribe.proofs.tools import _build_fixture, _probe_tool, run_tool_probes


def test_fixture_includes_transcript_segments(tmp_path: Path) -> None:
    """The fixture enriches the example report with transcript segments."""
    report, toolbelt = _build_fixture(tmp_path)
    assert "transcript_segments" in report
    assert len(report["transcript_segments"]) >= 1
    assert toolbelt.output_dir == tmp_path


def test_fixture_repo_file_readable(tmp_path: Path) -> None:
    """open_repo_file can read the seeded fixture repo file."""
    _report, toolbelt = _build_fixture(tmp_path)
    result = toolbelt.execute("open_repo_file", {"path": "README.md"})
    parsed = __import__("json").loads(result)
    assert "content" in parsed
    assert "# fixture repo" in parsed["content"]


def test_probe_tool_returns_proven_for_read_tool(tmp_path: Path) -> None:
    """A simple read tool executes against the fixture and stamps PROVEN."""
    _report, toolbelt = _build_fixture(tmp_path)
    receipt = _probe_tool(toolbelt, "list_findings")
    assert receipt.element == "list_findings"
    assert receipt.kind == "tool"
    assert receipt.verdict == VERDICT_PROVEN
    assert receipt.request_essence == "execute list_findings"


def test_probe_tool_handles_unknown_tool(tmp_path: Path) -> None:
    """An unknown tool name stamps NOT-PROVEN with a clear reason."""
    _report, toolbelt = _build_fixture(tmp_path)
    receipt = _probe_tool(toolbelt, "not_a_real_tool")
    assert receipt.verdict == "NOT-PROVEN"
    assert "Unknown tool" in receipt.reason


def test_run_tool_probes_covers_all_inventoryed_tools() -> None:
    """All 14 ReportToolbelt tools are probed and produce receipts."""
    receipts = run_tool_probes()
    elements = {r.element for r in receipts}
    assert elements == set(TOOL_NAMES)
    assert all(r.verdict == VERDICT_PROVEN for r in receipts)
