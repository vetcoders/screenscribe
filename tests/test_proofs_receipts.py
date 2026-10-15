"""Tests for the proof receipt schema, writer, and matrix renderer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from screenscribe.proofs.receipts import (
    ProofReceipt,
    render_matrix,
    run_proof,
    write_receipt,
)


def test_receipt_schema_matches_contract() -> None:
    """Receipt dict must match the frozen schema exactly."""
    receipt = ProofReceipt(
        element="test:element",
        kind="provider",
        request_essence="POST /ping",
        response_essence="pong",
        duration_ms=42,
        verdict="PROVEN",
        reason="",
        key_state="present",
    )
    data = receipt.to_dict()
    assert data["schema"] == "screenscribe.proof-receipt.v1"
    assert data["element"] == "test:element"
    assert data["kind"] == "provider"
    assert "ts" in data
    assert data["request_essence"] == "POST /ping"
    assert data["response_essence"] == "pong"
    assert data["duration_ms"] == 42
    assert data["verdict"] == "PROVEN"
    assert data["reason"] == ""
    assert data["key_state"] == "present"


def test_write_receipt_creates_atomic_json(tmp_path: Path) -> None:
    """write_receipt emits a single JSON file and leaves no temp detritus."""
    receipt = ProofReceipt(
        element="foo/bar:baz",
        kind="tool",
        request_essence="execute foo",
        response_essence="ok",
        duration_ms=1,
        verdict="PROVEN",
    )
    dest = write_receipt(tmp_path, receipt)
    assert dest.exists()
    assert dest.name == "foo_bar_baz.receipt.json"
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["element"] == "foo/bar:baz"
    assert data["verdict"] == "PROVEN"
    # No leftover temp files.
    assert not any(p.name.startswith(".receipt-") for p in tmp_path.iterdir())


def test_render_matrix_groups_providers_then_tools(tmp_path: Path) -> None:
    """MATRIX.md lists providers first, tools second, and includes totals."""
    receipts = [
        ProofReceipt(
            element="p2",
            kind="provider",
            request_essence="r",
            response_essence="ok",
            duration_ms=1,
            verdict="NOT-PROVEN",
            reason="bad",
        ),
        ProofReceipt(
            element="t1",
            kind="tool",
            request_essence="r",
            response_essence="ok",
            duration_ms=2,
            verdict="PROVEN",
        ),
        ProofReceipt(
            element="p1",
            kind="provider",
            request_essence="r",
            response_essence="ok",
            duration_ms=3,
            verdict="PROVEN",
        ),
    ]
    matrix_path = render_matrix(receipts, tmp_path)
    text = matrix_path.read_text(encoding="utf-8")
    lines = [
        line for line in text.splitlines() if line.startswith("|") and not line.startswith("|---")
    ]
    # Header + 3 data rows.
    assert len(lines) == 4
    data_lines = lines[1:]
    assert "p1" in data_lines[0]
    assert "p2" in data_lines[1]
    assert "t1" in data_lines[2]
    assert "3 elements, 2 PROVEN, 1 NOT-PROVEN" in text


def test_render_matrix_escapes_pipes(tmp_path: Path) -> None:
    """Pipe characters in reasons must not break the markdown table."""
    receipts = [
        ProofReceipt(
            element="x",
            kind="provider",
            request_essence="r",
            response_essence="ok",
            duration_ms=1,
            verdict="NOT-PROVEN",
            reason="a | b",
        )
    ]
    matrix_path = render_matrix(receipts, tmp_path)
    assert "a \\| b" in matrix_path.read_text(encoding="utf-8")


def test_run_proof_writes_all_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_proof writes one receipt per element plus matrix and optional summary."""
    from screenscribe.proofs import providers, tools

    def fake_provider_probes() -> list[ProofReceipt]:
        return [
            ProofReceipt(
                element="provider:a",
                kind="provider",
                request_essence="ping",
                response_essence="pong",
                duration_ms=1,
                verdict="PROVEN",
            )
        ]

    def fake_tool_probes() -> list[ProofReceipt]:
        return [
            ProofReceipt(
                element="tool:b",
                kind="tool",
                request_essence="ping",
                response_essence="pong",
                duration_ms=2,
                verdict="PROVEN",
            )
        ]

    monkeypatch.setattr(providers, "run_provider_probes", fake_provider_probes)
    monkeypatch.setattr(tools, "run_tool_probes", fake_tool_probes)

    receipts = run_proof(tmp_path, json_mode=True)
    assert len(receipts) == 2
    assert (tmp_path / "provider_a.receipt.json").exists()
    assert (tmp_path / "tool_b.receipt.json").exists()
    assert (tmp_path / "MATRIX.md").exists()
    assert (tmp_path / "summary.json").exists()

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema"] == "screenscribe.proof-summary.v1"
    assert summary["total"] == 2
    assert summary["proven"] == 2
