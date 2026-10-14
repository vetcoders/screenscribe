"""Tests for the tool proof harness (local, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    """Every ReportToolbelt tool is probed and produces a receipt."""
    receipts = run_tool_probes()
    elements = {r.element for r in receipts}
    assert elements == set(TOOL_NAMES)
    assert all(r.verdict == VERDICT_PROVEN for r in receipts)


def test_packaged_fixtures_used_without_repo_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed wheels ship fixtures inside the package (no repo checkout)."""
    import screenscribe.proofs.tools as tools_mod

    packaged = tmp_path / "pkg" / "fixtures"
    packaged.mkdir(parents=True)
    source = tools_mod._fixture_source()  # repo path in this checkout
    for name in (tools_mod._EXAMPLE_REPORT, tools_mod._EXAMPLE_TRANSCRIPT):
        (packaged / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_PACKAGED_FIXTURES", packaged)
    assert tools_mod._fixture_source() == packaged
    out = tmp_path / "out"
    out.mkdir()
    report, _toolbelt = tools_mod._build_fixture(out)
    assert report["transcript_segments"]


def test_missing_fixtures_stamp_not_proven_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fixtures anywhere -> all tools stamped NOT-PROVEN, no harness crash."""
    import screenscribe.proofs.tools as tools_mod

    def no_source() -> Path:
        raise FileNotFoundError("proof fixtures missing")

    monkeypatch.setattr(tools_mod, "_fixture_source", no_source)
    receipts = tools_mod.run_tool_probes()
    assert len(receipts) == len(TOOL_NAMES)
    assert all(r.verdict == "NOT-PROVEN" for r in receipts)
    assert all("fixtures missing" in r.reason for r in receipts)
