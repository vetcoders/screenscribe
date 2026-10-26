"""Screenscribe agent tools against the synthetic calculator report fixture (no video)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from screenscribe.agent.context import seed_report_context
from screenscribe.agent.moments import manual_markers
from screenscribe.agent.tools import ReportToolbelt, responses_tool_schemas

FIXTURE = Path(__file__).parent / "fixtures" / "agent_report_synthetic.json"


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


def test_manual_markers_are_discoverable_deduplicated_and_round_trip_by_id() -> None:
    report = {
        "findings": [
            {
                "id": 7,
                "category": "bug",
                "timestamp_start": 12.0,
                "text": "AI finding",
                "screenshot": "screenshots/ai.jpg",
                "unified_analysis": {"severity": "high", "summary": "AI summary"},
            }
        ],
        "manual_review": {
            "markers": [
                {
                    "marker_id": "manual-54",
                    "timestamp": 54.0,
                    "frame_path": "manual_frames/manual-54.jpg",
                    "notes": "First manual note",
                },
                {
                    "marker_id": "manual-63",
                    "timestamp": 63.0,
                    "frame_path": "manual_frames/manual-63.jpg",
                    "notes": "Second manual note",
                },
            ]
        },
        "human_review": {
            "manual_frames": [
                {"marker_id": "manual-54", "timestamp": 54.0, "notes": "UI alias"},
                {"id": "manual-63", "timestamp": 63.0, "notes": "legacy id alias"},
            ]
        },
    }
    belt = ReportToolbelt(report)

    result = belt.list_findings()
    assert result["count"] == 3
    assert result["finding_count"] == 1
    assert result["manual_marker_count"] == 2
    assert result["available_moment_count"] == 3
    assert result["total_count"] == 3
    assert result["findings"][0]["kind"] == "finding"
    manual_rows = [row for row in result["findings"] if row["kind"] == "manual_marker"]
    assert [row["id"] for row in manual_rows] == ["manual-54", "manual-63"]
    assert manual_rows[1]["frame_path"] == "manual_frames/manual-63.jpg"
    assert manual_rows[1]["notes"] == "Second manual note"

    filtered = belt.list_findings("manual_frames/manual-63")
    assert filtered["count"] == 1
    assert filtered["finding_count"] == 0
    assert filtered["manual_marker_count"] == 1
    discovered_id = filtered["findings"][0]["id"]
    detail = belt.get_finding(discovered_id)
    assert detail["kind"] == "manual_marker"
    assert detail["marker_id"] == "manual-63"
    assert detail["notes"] == "Second manual note"
    assert detail["frames"] == [{"frame": "manual_frames/manual-63.jpg", "timestamp": 63.0}]

    summary = belt.get_report_summary()
    assert summary["finding_count"] == 1
    assert summary["manual_marker_count"] == 2
    assert summary["available_moment_count"] == 3
    assert summary["total_count"] == 3


def test_manual_marker_normalization_merges_compat_sources_without_mutation() -> None:
    report = {
        "findings": [],
        "manual_review": {
            "markers": [
                {
                    "marker_id": "m2",
                    "timestamp": 54.0,
                    "notes": "user note",
                    "severity": "critical",
                }
            ],
            "results": [
                {
                    "marker_id": "m2",
                    "timestamp": 54.0,
                    "category": "bug",
                    "severity": "high",
                    "summary": "saved result summary",
                    "issues_detected": ["Contrast is too low"],
                    "suggested_fix": "Raise contrast",
                }
            ],
        },
        "human_review": {
            "manual_frames": [
                {
                    "marker_id": "m2",
                    "timestamp": 999.0,
                    "notes": "",
                    "severity": "",
                    "frame_path": "manual_frames/m2.jpg",
                    "result": {
                        "category": "ui",
                        "summary": "analysis from compatibility frame",
                    },
                }
            ]
        },
    }
    original = deepcopy(report)

    normalized = manual_markers(report)
    assert normalized == [
        {
            "marker_id": "m2",
            "timestamp": 54.0,
            "notes": "user note",
            "severity": "critical",
            "frame_path": "manual_frames/m2.jpg",
            "result": {
                "marker_id": "m2",
                "timestamp": 54.0,
                "category": "ui",
                "severity": "high",
                "summary": "analysis from compatibility frame",
                "issues_detected": ["Contrast is too low"],
                "suggested_fix": "Raise contrast",
            },
        }
    ]

    belt = ReportToolbelt(report)
    row = belt.list_findings()["findings"][0]
    assert row["id"] == "m2"
    assert row["marker_id"] == "m2"
    assert row["timestamp"] == 54.0
    assert row["notes"] == "user note"
    assert row["severity"] == "critical"
    assert row["frame_path"] == "manual_frames/m2.jpg"
    assert row["summary"] == "analysis from compatibility frame"

    detail = belt.get_finding(row["id"])
    assert detail["marker_id"] == "m2"
    assert detail["result"]["category"] == "ui"
    assert detail["result"]["issues_detected"] == ["Contrast is too low"]
    assert detail["result"]["suggested_fix"] == "Raise contrast"

    seed = json.loads(seed_report_context(report))
    assert seed["manual_markers"][0]["id"] == row["id"]
    assert seed["manual_markers"][0]["summary"] == row["summary"]
    assert seed["manual_markers"][0]["frame_path"] == row["frame_path"]
    assert report == original


def test_get_transcript_window() -> None:
    result = _toolbelt().get_transcript(start=3.0, end=8.0)
    assert result["count"] >= 1
    assert any("why" in str(seg.get("text") or "").lower() for seg in result["segments"])


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
    assert "Top priorities" in result["executive_summary"]
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


# Credential SHAPES, assembled at runtime: a literal one in this file would be
# a finding for the repo's own leak scan, which has no inline allowlist.
_SHAPE_KEY = "sk-" + "abcd1234efgh5678ijkl"
_SHAPE_PEM = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
_SHAPE_GITHUB = "ghp_" + "abcdefghijklmnopqrstuvwxyz"
_SHAPE_AWS = "AKIA" + "ZZ1234567890ABCD"
_SHAPE_ASSIGNED = "Zm9vYmFy" + "YmF6cXV4MTIzNA=="


@pytest.mark.parametrize(
    ("name", "body", "expected_lines"),
    [
        (
            "README.md",
            f"# docs\n\nUse {_SHAPE_KEY} for staging.\n",
            [3],
        ),
        (
            "notes.md",
            f"intro\n{_SHAPE_PEM}\nAAAABBBB\n",
            [2],
        ),
        (
            "deploy.md",
            f'Set it up:\n\n    api_key = "{_SHAPE_ASSIGNED}"\n',
            [3],
        ),
    ],
)
def test_open_repo_file_refuses_credential_looking_content(
    tmp_path: Path, name: str, body: str, expected_lines: list[int]
) -> None:
    """Whatever this tool returns goes straight to the provider: fail closed."""
    (tmp_path / name).write_text(body, encoding="utf-8")
    result = _toolbelt(repo_root=tmp_path).open_repo_file(name)
    assert result["error"] == (
        "file content looks like it contains credentials; not sent to the model"
    )
    assert result["lines"] == expected_lines
    assert "content" not in result
    # The reason names WHERE, never WHAT.
    rendered = json.dumps(result)
    for shape in (_SHAPE_KEY, _SHAPE_PEM, _SHAPE_ASSIGNED):
        assert shape not in rendered


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("settings.py", 'import os\n\nAPI_KEY = os.environ["X"]\n'),
        ("sample.md", "Copy the sample:\n\n    API_KEY=your-key-here\n"),
        ("config.md", "Use `API_TOKEN=changeme-before-deploy` locally.\n"),
    ],
)
def test_open_repo_file_allows_env_lookups_and_placeholders(
    tmp_path: Path, name: str, body: str
) -> None:
    """The gate must not swallow ordinary config docs and env-var reads."""
    (tmp_path / name).write_text(body, encoding="utf-8")
    result = _toolbelt(repo_root=tmp_path).open_repo_file(name)
    assert result["content"] == body
    assert "error" not in result


def test_content_looks_secret_reports_every_offending_line() -> None:
    from screenscribe.agent.tools import _content_looks_secret

    assert _content_looks_secret("clean\nlines only\n") == []
    flagged = _content_looks_secret(f"ok\n{_SHAPE_GITHUB}\nstill ok\n{_SHAPE_AWS}\n")
    assert flagged == [2, 4]


def test_repo_tool_schema_does_not_claim_nonexistent_cli_flag() -> None:
    schema = next(
        tool
        for tool in responses_tool_schemas(include_repo=True)
        if tool["name"] == "open_repo_file"
    )
    assert "--repo" not in schema["description"]
    assert "programmatically" in schema["description"]


def test_tool_failure_does_not_expose_private_exception_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolbelt = _toolbelt(repo_root=tmp_path)
    private_detail = f"debug path: {tmp_path / 'private-source.py'}"

    def fail_read(_path: str) -> dict[str, object]:
        raise OSError(private_detail)

    monkeypatch.setattr(toolbelt, "open_repo_file", fail_read)
    result = toolbelt.execute("open_repo_file", {"path": "source.py"})
    assert json.loads(result) == {"error": "Agent tool failed. Check server logs."}
    assert private_detail not in result


_CORRECTION_ID = "cor-11111111-1111-4111-8111-111111111111"


def test_corrections_ledger_round_trip_is_byte_exact(tmp_path: Path) -> None:
    """Add, save, reload: entry fields and file bytes survive a second atomic write."""
    from screenscribe.agent.corrections import append_correction, load_ledger, save_ledger

    output_dir = tmp_path / "review"
    output_dir.mkdir()
    glossary = tmp_path / "glossary"
    entry = append_correction(
        output_dir,
        match="  cloud  ",
        replacement="Claude",
        scope="report",
        note="agent voice -> Claude, not chmura",
        source="agent",
        report_stem="screen_report",
        glossary_dir=glossary,
        created_at="2026-09-28T21:00:00Z",
        entry_id=_CORRECTION_ID,
    )
    path = output_dir / "corrections.json"
    first = path.read_bytes()
    loaded = load_ledger(output_dir)
    assert loaded.warning is None
    assert loaded.report == "screen_report"
    assert loaded.entries == [entry]
    assert entry["match"] == "cloud"
    assert entry["replacement"] == "Claude"
    save_ledger(output_dir, loaded.entries, report=loaded.report, glossary_dir=glossary)
    assert path.read_bytes() == first
    assert not glossary.exists()


def test_corrections_do_not_rewrite_transcript_bytes(tmp_path: Path) -> None:
    """Ledger writes must leave the original transcript (and report JSON) untouched."""
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    transcript = output_dir / "transcript.vtt"
    transcript_bytes = b"WEBVTT\n\n00:00.000 --> 00:01.000\ncloud agent\n"
    transcript.write_bytes(transcript_bytes)
    report_json = output_dir / "screen_report.json"
    report_bytes = b'{"findings":[{"text":"cloud"}]}\n'
    report_json.write_bytes(report_bytes)

    belt = ReportToolbelt(
        {},
        output_dir=output_dir,
        report_stem="screen_report",
        glossary_dir=tmp_path / "glossary",
    )
    result = belt.correct_transcript("cloud", "Claude", "report", "heard chmura")
    assert result["type"] == "review_patch"
    assert result["ops"][0]["op"] == "correct_transcript"
    assert result["ops"][0]["persisted"] is True
    assert result["ops"][0]["match"] == "cloud"
    assert transcript.read_bytes() == transcript_bytes
    assert report_json.read_bytes() == report_bytes


def test_global_correction_is_a_pending_plan_not_a_write(tmp_path: Path) -> None:
    """A global correction edits the glossary every future analysis reads.

    The agent may propose it; only the panel's Apply may save it. Report scope
    stays an immediate ledger write.
    """
    from screenscribe.agent.tools import anthropic_tool_schemas, responses_tool_schemas

    output_dir = tmp_path / "review"
    output_dir.mkdir()
    glossary = tmp_path / "glossary"
    belt = ReportToolbelt(
        {},
        output_dir=output_dir,
        report_stem="screen_report",
        glossary_dir=glossary,
    )

    plan = belt.correct_transcript("cloud", "Claude", "global", "heard chmura")
    assert plan["type"] == "review_plan"
    assert plan["status"] == "pending_approval"
    assert plan["rationale"]
    op = plan["ops"][0]
    assert op["op"] == "correct_transcript"
    assert op["scope"] == "global"
    assert op["persisted"] is False
    assert op["requires_approval"] is True
    assert not (output_dir / "corrections.json").exists()
    assert not glossary.exists()

    patch = belt.correct_transcript("cloud", "Claude", "report")
    assert patch["type"] == "review_patch"
    assert patch["ops"][0]["persisted"] is True
    assert (output_dir / "corrections.json").exists()
    assert not glossary.exists()

    # The model must be told, or it will promise the user a save that is pending.
    tool = next(
        item
        for item in responses_tool_schemas(include_repo=False)
        if item["name"] == "correct_transcript"
    )
    assert "approves it in the review panel" in tool["description"]
    anth = next(
        item
        for item in anthropic_tool_schemas(include_repo=False)
        if item["name"] == "correct_transcript"
    )
    assert anth["description"] == tool["description"]


def test_correct_transcript_rejects_empty_match_scope_and_regex(tmp_path: Path) -> None:
    from screenscribe.agent.corrections import apply_transcript_overlay, correction_pieces
    from screenscribe.agent.tools import anthropic_tool_schemas, responses_tool_schemas

    belt = ReportToolbelt({}, output_dir=tmp_path, glossary_dir=tmp_path / "glossary")
    empty = belt.correct_transcript("   ", "Claude", "report")
    assert empty["error"] == "match must be a non-empty literal phrase"
    bad_scope = belt.correct_transcript("cloud", "Claude", "regex")
    assert "scope must be" in bad_scope["error"]
    assert not (tmp_path / "corrections.json").exists()

    stored = belt.correct_transcript("c.oud", "NO", "report")
    assert stored["ops"][0]["match"] == "c.oud"
    assert apply_transcript_overlay("cloud", [{"match": "c.oud", "replacement": "NO"}]) == "cloud"
    assert apply_transcript_overlay("cloudless", [{"match": "cloud", "replacement": "Claude"}]) == (
        "cloudless"
    )
    pieces = correction_pieces(
        "say Cloud.",
        [{"match": "cloud", "replacement": "Claude", "created_at": "2026-09-28T21:00:00Z"}],
    )
    replaced = [piece for piece in pieces if piece.original is not None]
    assert replaced == [type(pieces[0])("Claude", "Cloud")]
    later = apply_transcript_overlay(
        "the cloud agent",
        [
            {
                "match": "cloud agent",
                "replacement": "X",
                "created_at": "2026-09-28T21:00:00Z",
            },
            {
                "match": "cloud",
                "replacement": "Claude",
                "created_at": "2026-09-28T21:01:00Z",
            },
        ],
    )
    assert later == "the Claude agent"

    responses = responses_tool_schemas(include_repo=False)
    tool = next(item for item in responses if item["name"] == "correct_transcript")
    assert tool["parameters"]["required"] == ["match", "replacement", "scope"]
    assert tool["parameters"]["properties"]["scope"]["enum"] == ["report", "global"]
    anthropic = anthropic_tool_schemas(include_repo=False)
    anth = next(item for item in anthropic if item["name"] == "correct_transcript")
    assert anth["input_schema"] == tool["parameters"]


def test_global_glossary_dedups_match_replacement(tmp_path: Path) -> None:
    from screenscribe.agent.corrections import (
        append_correction,
        corrections_summary,
        load_glossary,
    )

    output_dir = tmp_path / "review"
    output_dir.mkdir()
    glossary = tmp_path / "cfg"
    append_correction(
        output_dir,
        match="cloud",
        replacement="Claude",
        scope="global",
        source="user",
        glossary_dir=glossary,
        created_at="2026-09-28T21:00:00Z",
        entry_id="cor-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    append_correction(
        output_dir,
        match="cloud",
        replacement="Claude",
        scope="global",
        note="newer",
        source="agent",
        glossary_dir=glossary,
        created_at="2026-09-28T22:00:00Z",
        entry_id="cor-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    append_correction(
        output_dir,
        match="chmura",
        replacement="Claude",
        scope="report",
        glossary_dir=glossary,
        created_at="2026-09-28T23:00:00Z",
        entry_id="cor-cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )
    loaded = load_glossary(glossary)
    assert loaded.warning is None
    assert [(entry["match"], entry["created_at"]) for entry in loaded.entries] == [
        ("cloud", "2026-09-28T22:00:00Z")
    ]
    summary = corrections_summary(output_dir, limit=20, glossary_dir=glossary)
    assert summary["warning"] is None
    assert summary["count"] == 2
    assert summary["truncated"] is False
    assert [entry["match"] for entry in summary["entries"]] == ["cloud", "chmura"]


def test_ledger_replace_failure_keeps_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import screenscribe.agent.corrections as corrections

    output_dir = tmp_path / "review"
    output_dir.mkdir()
    glossary = tmp_path / "glossary"
    corrections.append_correction(
        output_dir,
        match="cloud",
        replacement="Claude",
        scope="report",
        glossary_dir=glossary,
        created_at="2026-09-28T21:00:00Z",
        entry_id=_CORRECTION_ID,
    )
    path = output_dir / "corrections.json"
    first = path.read_bytes()

    def boom(_src: str, _dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(corrections.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        corrections.append_correction(
            output_dir,
            match="chmura",
            replacement="Claude",
            scope="report",
            glossary_dir=glossary,
            created_at="2026-09-28T22:00:00Z",
            entry_id="cor-dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        )
    assert path.read_bytes() == first
    assert list(output_dir.glob(".corrections-*.json.tmp")) == []


def test_append_correction_retry_is_idempotent(tmp_path: Path) -> None:
    """R2-06: a retried save must not duplicate the ledger row.

    The report ledger is written before the glossary roll-up; when the second
    write fails the API reports an error and the client retries. The retry
    now returns the stored entry instead of appending a twin.
    """
    from screenscribe.agent.corrections import append_correction, load_ledger

    glossary = tmp_path / "glossary"
    first = append_correction(
        tmp_path,
        match="zeto",
        replacement="ZETO",
        scope="global",
        report_stem="r",
        glossary_dir=glossary,
    )
    second = append_correction(
        tmp_path,
        match="zeto",
        replacement="ZETO",
        scope="global",
        report_stem="r",
        glossary_dir=glossary,
    )
    assert second["id"] == first["id"]
    ledger = load_ledger(tmp_path)
    assert [e["match"] for e in ledger.entries] == ["zeto"]


def test_failed_glossary_roll_up_degrades_and_heals_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-06: a failed glossary write is a partial save, not a total failure.

    First attempt: the ledger write lands, the glossary write raises. The call
    still returns the entry (the correction IS saved for this report). The
    retry finds the existing row and re-runs the roll-up, healing the glossary.
    """
    from screenscribe.agent import corrections as corrections_module
    from screenscribe.agent.corrections import append_correction, load_glossary, load_ledger

    glossary = tmp_path / "glossary"
    real_write = corrections_module._write_json_atomic
    fail_once = {"armed": True}

    def flaky_write(path: Path, payload: dict[str, Any]) -> None:
        if fail_once["armed"] and path.name == "corrections_glossary.json":
            fail_once["armed"] = False
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(corrections_module, "_write_json_atomic", flaky_write)
    entry = append_correction(
        tmp_path,
        match="zeto",
        replacement="ZETO",
        scope="global",
        report_stem="r",
        glossary_dir=glossary,
    )
    assert entry["match"] == "zeto"
    assert load_ledger(tmp_path).entries  # report ledger has the row
    assert not (glossary / "corrections_glossary.json").exists()

    retry = append_correction(
        tmp_path,
        match="zeto",
        replacement="ZETO",
        scope="global",
        report_stem="r",
        glossary_dir=glossary,
    )
    assert retry["id"] == entry["id"]
    assert [e["match"] for e in load_ledger(tmp_path).entries] == ["zeto"]
    glossary_load = load_glossary(glossary)
    assert [e["match"] for e in glossary_load.entries] == ["zeto"]


def test_correction_phrases_have_an_input_size_cap(tmp_path: Path) -> None:
    """R2-05 input side: a 3000-char 'phrase' is not a transcript correction."""
    from screenscribe.agent.corrections import CorrectionRejected, append_correction

    with pytest.raises(CorrectionRejected, match="longer than"):
        append_correction(
            tmp_path,
            match="x" * 3000,
            replacement="y",
            scope="report",
            report_stem="r",
        )
    with pytest.raises(CorrectionRejected, match="longer than"):
        append_correction(
            tmp_path,
            match="x",
            replacement="y" * 3000,
            scope="report",
            report_stem="r",
        )
