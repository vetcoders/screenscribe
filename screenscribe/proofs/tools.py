"""Execute every ReportToolbelt tool against a fixture report and stamp receipts."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..agent.tools import TOOL_NAMES, ReportToolbelt
from .receipts import VERDICT_NOT_PROVEN, VERDICT_PROVEN, ProofReceipt

# Fixture filenames; resolved against the packaged fixtures/ dir (installed
# wheel) first and the repo's examples/ dir (dev checkout) second.
_EXAMPLE_REPORT = "example_report.json"
_EXAMPLE_TRANSCRIPT = "example_transcript.vtt"
_PACKAGED_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_source() -> Path:
    """Directory holding the proof fixtures.

    Packaged ``fixtures/`` ships inside the wheel, so ``screenscribe doctor``
    works for a pipx/uv-installed user with no repo checkout around; the repo
    ``examples/`` path keeps dev checkouts exercising the canonical copies.
    """
    if (_PACKAGED_FIXTURES / _EXAMPLE_REPORT).exists():
        return _PACKAGED_FIXTURES
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent, here.parent.parent):
        if (candidate / "examples" / _EXAMPLE_REPORT).exists():
            return candidate / "examples"
    raise FileNotFoundError(
        "proof fixtures missing: neither packaged fixtures/ nor repo examples/ "
        "carry example_report.json"
    )


def _parse_vtt(path: Path) -> list[dict[str, Any]]:
    """Minimal WebVTT -> transcript segment list for the fixture."""
    text = path.read_text(encoding="utf-8")
    segments: list[dict[str, Any]] = []
    cue_re = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\s*\n"
        r"(.+?)(?=\n\n|\Z)",
        re.S,
    )

    def _to_seconds(ts: str) -> float:
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    for match in cue_re.finditer(text):
        idx, start, end, body = match.groups()
        segments.append(
            {
                "id": int(idx),
                "start": _to_seconds(start),
                "end": _to_seconds(end),
                "text": " ".join(body.splitlines()).strip(),
            }
        )
    return segments


def _build_fixture(tmp_dir: Path) -> tuple[dict[str, Any], ReportToolbelt]:
    """Copy example report + transcript into ``tmp_dir`` and return toolbelt."""
    source = _fixture_source()
    report_path = source / _EXAMPLE_REPORT
    transcript_path = source / _EXAMPLE_TRANSCRIPT

    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["transcript_segments"] = _parse_vtt(transcript_path)

    # Write a canonical report.json in the fixture dir so tools see it.
    fixture_report_path = tmp_dir / "report.json"
    fixture_report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Seed a repo file for open_repo_file.
    repo_root = tmp_dir / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "README.md").write_text("# fixture repo\n", encoding="utf-8")

    toolbelt = ReportToolbelt(
        report=report,
        output_dir=tmp_dir,
        repo_root=repo_root,
        report_stem="example_report",
        glossary_dir=tmp_dir / "glossary",
    )
    return report, toolbelt


def _probe_tool(toolbelt: ReportToolbelt, name: str) -> ProofReceipt:
    """Execute one tool with a sensible ping payload and build a receipt."""
    import time

    start = time.perf_counter()
    try:
        if name == "list_findings":
            result = toolbelt.execute(name, {"filter": "bug"})
        elif name == "get_transcript":
            result = toolbelt.execute(name, {"start": 0.0, "end": 10.0})
        elif name == "seek":
            result = toolbelt.execute(name, {"timestamp": 5.0})
        elif name == "show_frame":
            result = toolbelt.execute(name, {"finding_id": 1})
        elif name == "get_report_summary":
            result = toolbelt.execute(name, {})
        elif name == "open_repo_file":
            result = toolbelt.execute(name, {"path": "README.md"})
        elif name == "set_verdict":
            result = toolbelt.execute(name, {"finding_id": 1, "verdict": "accepted"})
        elif name == "set_severity":
            result = toolbelt.execute(name, {"finding_id": 1, "severity": "high"})
        elif name == "edit_finding":
            result = toolbelt.execute(
                name, {"finding_id": 1, "summary": "Proof edit summary"}
            )
        elif name == "add_finding":
            result = toolbelt.execute(
                name,
                {
                    "timestamp": 7.0,
                    "summary": "Proof finding",
                    "severity": "medium",
                    "category": "ui",
                },
            )
        elif name == "merge_findings":
            result = toolbelt.execute(name, {"survivor_id": 1, "member_ids": [2]})
        elif name == "unmerge_finding":
            result = toolbelt.execute(name, {"survivor_id": 1})
        elif name == "correct_transcript":
            result = toolbelt.execute(
                name,
                {
                    "match": "toolbar",
                    "replacement": "tool bar",
                    "scope": "report",
                    "note": "proof correction",
                },
            )
        elif name == "propose_review":
            result = toolbelt.execute(
                name,
                {
                    "instruction": "Accept all bugs",
                    "ops": [{"op": "set_verdict", "finding_id": 1, "verdict": "accepted"}],
                    "rationale": ["accept bug"],
                },
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("error"):
            raise RuntimeError(parsed["error"])
        duration_ms = int((time.perf_counter() - start) * 1000)
        essence = "ok"
        if isinstance(parsed, dict):
            if "type" in parsed:
                essence = f"type={parsed['type']}"
            elif "count" in parsed:
                essence = f"count={parsed['count']}"
            elif "action" in parsed:
                essence = f"action={parsed['action']}"
        return ProofReceipt(
            element=name,
            kind="tool",
            request_essence=f"execute {name}",
            response_essence=essence,
            duration_ms=duration_ms,
            verdict=VERDICT_PROVEN,
            key_state="n/a",
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        return ProofReceipt(
            element=name,
            kind="tool",
            request_essence=f"execute {name}",
            response_essence="failed",
            duration_ms=duration_ms,
            verdict=VERDICT_NOT_PROVEN,
            reason=f"{type(exc).__name__}: {exc}",
            key_state="n/a",
        )


def run_tool_probes() -> list[ProofReceipt]:
    """Run all 14 ReportToolbelt tools against the example fixture.

    A missing fixture stamps every tool NOT-PROVEN with the reason instead of
    killing the whole doctor run — the harness exists to surface truth, and a
    crashed harness surfaces nothing.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="screenscribe-proof-tools-"))
    try:
        try:
            _report, toolbelt = _build_fixture(tmp_dir)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            return [
                ProofReceipt(
                    element=name,
                    kind="tool",
                    request_essence=f"execute {name}",
                    response_essence="fixture unavailable",
                    duration_ms=0,
                    verdict=VERDICT_NOT_PROVEN,
                    reason=reason,
                    key_state="n/a",
                )
                for name in TOOL_NAMES
            ]
        return [_probe_tool(toolbelt, name) for name in TOOL_NAMES]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
