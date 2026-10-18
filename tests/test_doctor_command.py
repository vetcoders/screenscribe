"""``screenscribe doctor`` CLI surface: help, artifact wiring, exit codes.

The probe logic itself is covered in ``tests/test_proofs_*.py``; these tests
pin the command name, the ``--out`` hand-off, and the exit-code contract
(0 = all proven, 2 = at least one NOT-PROVEN, 1 = harness failure).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli
from screenscribe.cli import app
from screenscribe.proofs.receipts import ProofReceipt

runner = CliRunner()


def _receipt(element: str, verdict: str) -> ProofReceipt:
    return ProofReceipt(
        element=element,
        kind="tool",
        ts="2026-10-01T00:00:00+00:00",
        request_essence="ping",
        response_essence="pong",
        duration_ms=1,
        verdict=verdict,
        reason="" if verdict == "PROVEN" else "boom",
        key_state="n/a",
    )


def test_doctor_help_names_the_command() -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "provider" in result.output.lower()
    assert "MATRIX.md" in result.output


def test_doctor_passes_out_dir_and_exits_zero_when_all_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Path] = {}

    def fake_run_proof(out: Path, *, json_mode: bool) -> list[ProofReceipt]:
        seen["out"] = out
        return [_receipt("list_findings", "PROVEN")]

    monkeypatch.setattr(cli, "run_proof", fake_run_proof)
    result = runner.invoke(app, ["doctor", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen["out"] == tmp_path
    assert "1 PROVEN" in result.output


def test_doctor_exits_two_when_any_element_not_proven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "run_proof",
        lambda out, *, json_mode: [_receipt("stt_xai", "NOT-PROVEN")],
    )
    result = runner.invoke(app, ["doctor", "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "NOT-PROVEN" in result.output


def test_doctor_exits_one_on_harness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(out: Path, *, json_mode: bool) -> list[ProofReceipt]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_proof", boom)
    result = runner.invoke(app, ["doctor", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "Doctor run failed" in result.output


def test_doctor_harness_failure_does_not_echo_private_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(out: Path, *, json_mode: bool) -> list[ProofReceipt]:
        raise RuntimeError(
            "synthetic-account-token https://user:password@example.test?private"  # pragma: allowlist secret
        )

    monkeypatch.setattr(cli, "run_proof", fail)
    result = runner.invoke(app, ["doctor", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "RuntimeError" in result.output
    for private in ("synthetic-account-token", "password", "private"):
        assert private not in result.output
