"""Release verification rejects dirty or mismatched installed provenance."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import release_artifacts_verify as verify


@pytest.mark.parametrize(
    "suffix,accepted",
    [
        ("+gabc1234", True),
        ("+gabc1234-dirty", False),
        ("+gabc1234ffff", False),
        ("+gfffffff", False),
        ("", False),
    ],
)
def test_installed_wheel_must_identify_exact_clean_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, accepted: bool
) -> None:
    def fake_run(*args: str, **kwargs: object) -> SimpleNamespace:
        if "--version" in args:
            return SimpleNamespace(stdout=f"Screenscribe v0.1.20{suffix}\n")
        if "-c" in args:
            return SimpleNamespace(stdout="0.1.20\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(verify, "run", fake_run)
    monkeypatch.setattr(verify, "installed_assets", lambda *_args: {"screenscribe/example.html"})
    if accepted:
        verify.verify_install(
            tmp_path / "sample.whl",
            "wheel",
            "0.1.20",
            {"screenscribe/example.html"},
            render_smoke=False,
            expect_commit="abc1234",
        )
    else:
        with pytest.raises(RuntimeError, match="expected commit"):
            verify.verify_install(
                tmp_path / "sample.whl",
                "wheel",
                "0.1.20",
                {"screenscribe/example.html"},
                render_smoke=False,
                expect_commit="abc1234",
            )
