"""Unit tests for cli.py review-command core flow, helpers, and config-show.

These pin CLI-surface behavior (arg parsing, config resolution, exit codes,
output routing) without running the heavy pipeline. Only true externals are
mocked: ffmpeg checks, audio extraction, duration probing, model validation,
the uvicorn server, the browser, and subprocess. Every test names the exact
behavior it pins.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
import typer
import uvicorn
from typer.testing import CliRunner

import screenscribe.cli as cli
import screenscribe.review_server as review_server
import screenscribe.server_security as server_security
from screenscribe.cli import (
    _find_next_review_path,
    _find_next_versioned_path,
    _interactive_mode,
    _serve_report,
    _show_estimate,
    app,
    version_callback,
)
from screenscribe.config import ScreenScribeConfig
from screenscribe.validation import APIKeyError, ModelValidationError

runner = CliRunner()


# --------------------------------------------------------------------------- #
# version_callback / version command
# --------------------------------------------------------------------------- #


def test_version_callback_exits_when_true() -> None:
    """--version eager callback raises typer.Exit so no command runs."""
    with pytest.raises(typer.Exit):
        version_callback(True)


def test_version_callback_noop_when_false() -> None:
    """A falsey --version must not exit; normal flow continues."""
    assert version_callback(False) is None


def test_version_command_prints_brand() -> None:
    """`version` prints the Vetcoders byline and exits 0."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Screenscribe" in result.output
    assert "Vetcoders" in result.output


# --------------------------------------------------------------------------- #
# _find_next_versioned_path / _find_next_review_path
# --------------------------------------------------------------------------- #


def test_find_next_review_path_returns_base_when_empty(tmp_path: Path) -> None:
    """No prior artifact bundle -> base path is reused, version is None."""
    base = tmp_path / "vid_review"
    path, version = _find_next_review_path(base)
    assert path == base
    assert version is None


def test_find_next_review_path_versions_when_bundle_present(tmp_path: Path) -> None:
    """An existing report.html marks a completed bundle -> bump to _2."""
    base = tmp_path / "vid_review"
    base.mkdir()
    (base / "report.html").write_text("done")
    path, version = _find_next_review_path(base)
    assert path == tmp_path / "vid_review_2"
    assert version == 2


def test_find_next_review_path_detects_stemmed_report_bundle(tmp_path: Path) -> None:
    """Completed reviews are written as <stem>_report.* -- they MUST be detected.

    Regression: the marker list used the old non-stemmed report.html/report.json,
    so a folder holding clip_report.html was treated as empty and the next run
    re-entered it and overwrote the existing review (data loss).
    """
    base = tmp_path / "clip_review"
    base.mkdir()
    (base / "clip_report.html").write_text("done")
    (base / "clip_report.json").write_text("{}")
    path, version = _find_next_review_path(base)
    assert path == tmp_path / "clip_review_2"
    assert version == 2


def test_find_next_review_path_detects_markdown_only_bundle(tmp_path: Path) -> None:
    """A bundle proven only by <stem>_report.md still counts as completed."""
    base = tmp_path / "clip_review"
    base.mkdir()
    (base / "clip_report.md").write_text("# done")
    path, version = _find_next_review_path(base)
    assert path == tmp_path / "clip_review_2"
    assert version == 2


def test_find_next_versioned_path_skips_consecutive_bundles(tmp_path: Path) -> None:
    """When _2 also holds a bundle, the next free slot is _3."""
    base = tmp_path / "out"
    base.mkdir()
    (base / "report.json").write_text("{}")
    (tmp_path / "out_2").mkdir()
    (tmp_path / "out_2" / "report.json").write_text("{}")
    path, version = _find_next_versioned_path(base, artifact_markers=("report.json",))
    assert path == tmp_path / "out_3"
    assert version == 3


def test_find_next_versioned_path_raises_past_cap(tmp_path: Path, monkeypatch: Any) -> None:
    """Past MAX_REVIEW_VERSIONS the helper refuses rather than loop forever."""
    monkeypatch.setattr(cli, "MAX_REVIEW_VERSIONS", 2)
    base = tmp_path / "cap"
    base.mkdir()
    (base / "report.json").write_text("{}")
    for n in (2, 3):
        d = tmp_path / f"cap_{n}"
        d.mkdir()
        (d / "report.json").write_text("{}")
    with pytest.raises(RuntimeError, match="Too many review versions"):
        _find_next_versioned_path(base, artifact_markers=("report.json",))


# --------------------------------------------------------------------------- #
# review: input validation / error paths (exit codes)
# --------------------------------------------------------------------------- #


def test_review_missing_video_exits_1() -> None:
    """A non-existent video path is rejected before any heavy work."""
    result = runner.invoke(app, ["review", "/no/such/video.mov", "--no-serve"])
    assert result.exit_code == 1
    assert "Video not found" in result.output


def test_review_directory_input_exits_1(tmp_path: Path) -> None:
    """A directory passed where a video is expected fails with a clear error."""
    d = tmp_path / "adir.mov"
    d.mkdir()
    result = runner.invoke(app, ["review", str(d), "--no-serve"])
    assert result.exit_code == 1
    assert "directory" in result.output.lower()


def test_review_ffmpeg_missing_exits_1(tmp_path: Path, monkeypatch: Any) -> None:
    """When FFmpeg is absent the review command stops with exit code 1."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    def raise_ffmpeg() -> None:
        raise cli.FFmpegNotFoundError("ffmpeg not on PATH")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", raise_ffmpeg)
    result = runner.invoke(app, ["review", str(video), "--no-serve"])
    assert result.exit_code == 1
    assert "ffmpeg not on PATH" in result.output


def test_review_config_validation_error_exits_1(tmp_path: Path, monkeypatch: Any) -> None:
    """config.validate() warnings are fatal: each is printed, exit code 1."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)

    class _Cfg(ScreenScribeConfig):
        def validate(self, providers: set[str] | None = None) -> list[str]:
            assert providers == {"llm", "vision", "stt"}
            return ["bad endpoint scheme"]

    monkeypatch.setattr(cli.ScreenScribeConfig, "load", classmethod(lambda _c: _Cfg()))
    result = runner.invoke(app, ["review", str(video), "--no-serve"])
    assert result.exit_code == 1
    assert "Config Error" in result.output
    assert "bad endpoint scheme" in result.output


def test_review_no_key_api_error_exits_1(tmp_path: Path, monkeypatch: Any) -> None:
    """A missing API key surfaces validate_models' APIKeyError as exit 1."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    def raise_key(*_a: object, **_k: object) -> None:
        raise APIKeyError("No API key configured. Set SCREENSCRIBE_API_KEY")

    monkeypatch.setattr(cli, "validate_models", raise_key)
    result = runner.invoke(app, ["review", str(video), "--no-serve"])
    assert result.exit_code == 1
    assert "API Key Error" in result.output
    assert "SCREENSCRIBE_API_KEY" in result.output


def test_review_model_validation_error_prints_tip(tmp_path: Path, monkeypatch: Any) -> None:
    """A ModelValidationError exits 1 and points at the right config env var."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    def raise_model(*_a: object, **_k: object) -> None:
        raise ModelValidationError("vision model missing", model_type="vision", model_name="vlm-x")

    monkeypatch.setattr(cli, "validate_models", raise_model)
    result = runner.invoke(app, ["review", str(video), "--no-serve"])
    assert result.exit_code == 1
    assert "Model Error" in result.output
    assert "SCREENSCRIBE_VISION_MODEL" in result.output


# --------------------------------------------------------------------------- #
# review --estimate branch (no API key required, no pipeline)
# --------------------------------------------------------------------------- #


def test_review_estimate_skips_validation_and_prints_table(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """--estimate prints the time table and never calls validate_models."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(cli, "get_video_duration", lambda _v: 300.0)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("validate_models must not run under --estimate")

    monkeypatch.setattr(cli, "validate_models", boom)
    result = runner.invoke(app, ["review", str(video), "--estimate"])
    assert result.exit_code == 0, result.output
    assert "Estimated Processing Time" in result.output
    assert "Total estimated time" in result.output


def test_review_estimate_always_runs_the_semantic_prefilter(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Detection is always the semantic prefilter; the estimate has no filter level.

    The old ``--keywords-only`` flag (and the filter-level concept it selected)
    has been removed; keyword-only detection is no longer a product mode, so
    ``_show_estimate`` no longer takes a ``filter_level`` and the estimate always
    shows the semantic pre-filter row.
    """
    import inspect

    # The filter-level parameter is gone for good (single live detection path).
    assert "filter_level" not in inspect.signature(cli._show_estimate).parameters

    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(cli, "get_video_duration", lambda _v: 120.0)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    result = runner.invoke(app, ["review", str(video), "--estimate"])
    assert result.exit_code == 0, result.output
    assert "Semantic pre-filter" in result.output


def test_review_rejects_removed_keywords_only_flag(tmp_path: Path, monkeypatch: Any) -> None:
    """The removed ``--keywords-only`` flag is no longer accepted."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    result = runner.invoke(app, ["review", str(video), "--estimate", "--keywords-only"])
    assert result.exit_code != 0


def test_review_empty_keywords_file_does_not_break_invocation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An empty ``--keywords-file`` is a safe no-op: review still runs.

    Keywords are always-on AI hints; an empty dictionary must not crash the
    pipeline. We drive the no-API-key ``--estimate`` path so the real
    ``KeywordsConfig.load`` runs against the empty file without mocking it.
    """
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")
    empty_kw = tmp_path / "empty.yaml"
    empty_kw.write_text("")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(cli, "get_video_duration", lambda _v: 120.0)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )

    result = runner.invoke(
        app, ["review", str(video), "--estimate", "--keywords-file", str(empty_kw)]
    )
    assert result.exit_code == 0, result.output
    assert "Estimated Processing Time" in result.output


def test_analyze_empty_keywords_file_loads_without_breaking(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An empty ``--keywords-file`` is a safe no-op for analyze too.

    ``analyze`` loads keywords into the live server's config before validating
    the API key. With no key configured we reach the API-key error (exit 1),
    proving the empty dictionary loaded cleanly rather than crashing the
    command. The active vocabulary reaches the analyze session, not only review.
    """
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")
    empty_kw = tmp_path / "empty.yaml"
    empty_kw.write_text("")

    class _NoKeyCfg(ScreenScribeConfig):
        def get_vision_api_key(self) -> str:
            return ""

    monkeypatch.setattr(cli.ScreenScribeConfig, "load", classmethod(lambda _c: _NoKeyCfg()))

    result = runner.invoke(app, ["analyze", str(video), "--keywords-file", str(empty_kw)])
    assert result.exit_code == 1, result.output
    assert "API key required" in result.output


def test_review_estimate_handles_unknown_duration(tmp_path: Path, monkeypatch: Any) -> None:
    """A duration probe failure degrades to a warning, not a crash."""
    video = tmp_path / "v.mov"
    video.write_bytes(b"x")

    def raise_duration(_v: Path) -> float:
        raise RuntimeError("ffprobe failed")

    monkeypatch.setattr(cli, "check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr(cli, "_require_audio_or_exit", lambda _v: None)
    monkeypatch.setattr(cli, "get_video_duration", raise_duration)
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )
    result = runner.invoke(app, ["review", str(video), "--estimate"])
    assert result.exit_code == 0, result.output
    assert "Could not determine video duration" in result.output


# --------------------------------------------------------------------------- #
# _show_estimate helper directly
# --------------------------------------------------------------------------- #


def test_show_estimate_unified_path(capsys: Any) -> None:
    """Unified VLM estimate names the unified analysis row and the tip."""
    _show_estimate(120.0, vision=True, use_unified=True)
    out = capsys.readouterr().out
    assert "Unified VLM analysis" in out
    assert "unified VLM pipeline" in out


def test_show_estimate_legacy_separate_rows(capsys: Any) -> None:
    """Legacy (non-unified) path emits separate semantic + vision rows."""
    _show_estimate(180.0, vision=True, use_unified=False)
    out = capsys.readouterr().out
    assert "Semantic analysis" in out
    assert "Vision analysis" in out


def test_show_estimate_no_vision_tip(capsys: Any) -> None:
    """With AI analysis off, the fast-path tip is shown."""
    _show_estimate(60.0, vision=False, use_unified=True)
    out = capsys.readouterr().out
    assert "very fast" in out


def test_show_estimate_adds_prefilter_row(capsys: Any) -> None:
    """The estimate always shows the semantic pre-filter row (single detection path)."""
    _show_estimate(120.0, vision=True)
    out = capsys.readouterr().out
    assert "Semantic pre-filter" in out


# --------------------------------------------------------------------------- #
# _serve_report helper
# --------------------------------------------------------------------------- #


def test_serve_report_skips_when_no_report(tmp_path: Path) -> None:
    """No <stem>_report.html present -> the server is skipped, not started."""
    out = tmp_path / "out"
    out.mkdir()
    video = tmp_path / "clip.mov"
    video.write_bytes(b"x")
    # Should return cleanly without importing/serving anything.
    _serve_report(out, video, port=9001)
    # No symlink created, no crash.
    assert not (out / "clip.mov").exists()


def test_serve_report_starts_server_and_opens_browser(tmp_path: Path, monkeypatch: Any) -> None:
    """With a report present, _serve_report wires the app, opens the browser,
    and calls uvicorn.run on the chosen port; the video symlink is cleaned up."""
    out = tmp_path / "out"
    out.mkdir()
    video = tmp_path / "clip.mov"
    video.write_bytes(b"x")
    (out / "clip_report.html").write_text("<html></html>")

    opened: list[str] = []
    served: dict[str, Any] = {}

    class _FakeAppState:
        session_token = "tok"  # noqa: S105 - test fixture

    class _FakeApp:
        state = _FakeAppState()

    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))

    monkeypatch.setattr(review_server, "create_review_app", lambda **_k: _FakeApp())
    monkeypatch.setattr(server_security, "tokenized_url", lambda url, _t: url + "#tok")

    # Pin the port probe so the assertion tests wiring, not the runner's global
    # port state: production _serve_report fall-forwards off a busy port, so a
    # process already on 9123 (parallel test / stray listener) would otherwise
    # flake this "== 9123" assert. Identity probe = the preferred port is free.
    monkeypatch.setattr(cli, "_find_available_port", lambda preferred, **_k: preferred)

    def fake_run(app_arg: Any, **kwargs: Any) -> None:
        served["port"] = kwargs.get("port")
        served["host"] = kwargs.get("host")

    monkeypatch.setattr(uvicorn, "run", fake_run)

    _serve_report(out, video, port=9123)

    assert served["host"] == "127.0.0.1"
    assert served["port"] == 9123
    assert opened and opened[0].endswith("#tok")
    # symlink cleaned up in finally
    assert not (out / "clip.mov").is_symlink()


def test_serve_report_falls_forward_when_port_busy(tmp_path: Path, monkeypatch: Any) -> None:
    """A busy preferred port is reported and the next free port is used."""
    out = tmp_path / "out"
    out.mkdir()
    video = tmp_path / "clip.mov"
    video.write_bytes(b"x")
    (out / "clip_report.html").write_text("<html></html>")

    # Occupy the preferred port for the duration of the call.
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    preferred = busy.getsockname()[1]

    class _FakeAppState:
        session_token = "tok"  # noqa: S105 - test fixture

    class _FakeApp:
        state = _FakeAppState()

    served: dict[str, Any] = {}
    monkeypatch.setattr(cli.webbrowser, "open", lambda _u: None)
    monkeypatch.setattr(review_server, "create_review_app", lambda **_k: _FakeApp())
    monkeypatch.setattr(server_security, "tokenized_url", lambda url, _t: url)
    monkeypatch.setattr(uvicorn, "run", lambda _a, **k: served.update(port=k.get("port")))

    try:
        _serve_report(out, video, port=preferred)
    finally:
        busy.close()

    assert served["port"] != preferred
    # The chosen port is strictly after the busy one (fall-forward search).
    assert served["port"] > preferred


# --------------------------------------------------------------------------- #
# _interactive_mode helper
# --------------------------------------------------------------------------- #


def test_interactive_mode_version_choice_exits(monkeypatch: Any) -> None:
    """Choosing '5' (version) prints version and raises typer.Exit."""
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *_a, **_k: "5"))
    with pytest.raises(typer.Exit):
        _interactive_mode()


def test_interactive_mode_config_choice_runs_subprocess(monkeypatch: Any) -> None:
    """Choosing '6' (config) shells out to `config --show` then exits."""
    calls: list[list[str]] = []
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *_a, **_k: "6"))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, *_a, **_k: calls.append(argv))
    with pytest.raises(typer.Exit):
        _interactive_mode()
    assert calls and "config" in calls[0] and "--show" in calls[0]


def test_interactive_mode_keywords_choice_runs_subprocess(monkeypatch: Any) -> None:
    """Choosing '5' (keywords) shells out to `keywords list` then exits.

    Keywords must be discoverable in the interactive menu, not only in --help.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *_a, **_k: "5"))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, *_a, **_k: calls.append(argv))
    with pytest.raises(typer.Exit):
        _interactive_mode()
    assert calls and "keywords" in calls[0] and "list" in calls[0]


def test_interactive_mode_empty_path_exits_1(monkeypatch: Any) -> None:
    """Selecting review then giving no path is a clean exit(1)."""
    answers = iter(["1", "   "])
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *_a, **_k: next(answers)))
    with pytest.raises(typer.Exit) as exc:
        _interactive_mode()
    assert exc.value.exit_code == 1


def test_interactive_mode_missing_path_exits_1(monkeypatch: Any) -> None:
    """A path that does not exist is rejected with exit(1)."""
    answers = iter(["1", "/definitely/not/here.mov"])
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *_a, **_k: next(answers)))
    with pytest.raises(typer.Exit) as exc:
        _interactive_mode()
    assert exc.value.exit_code == 1


def test_interactive_mode_runs_selected_command(tmp_path: Path, monkeypatch: Any) -> None:
    """A real video path triggers a subprocess for the chosen command."""
    video = tmp_path / "clip.mov"
    video.write_bytes(b"x")
    answers = iter(["4", str(video)])  # 4 -> transcribe
    calls: list[list[str]] = []
    monkeypatch.setattr("rich.prompt.Prompt.ask", staticmethod(lambda *_a, **_k: next(answers)))
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, *_a, **_k: calls.append(argv))
    _interactive_mode()
    assert calls and "transcribe" in calls[0]
    assert str(video) in calls[0]


# --------------------------------------------------------------------------- #
# config command paths
# --------------------------------------------------------------------------- #


def test_config_no_flags_prints_usage_hint() -> None:
    """Bare `config` prints the usage hint listing the flags."""
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "--show" in result.output
    assert "--set-key" in result.output


def test_config_set_key_saves_and_reports_path(tmp_path: Path, monkeypatch: Any) -> None:
    """--set-key stores the key and prints the saved-config path."""
    saved: dict[str, str] = {}
    cfg_path = tmp_path / "cfg.env"

    class _Cfg(ScreenScribeConfig):
        def save_api_key(self, api_key: str) -> Path:
            saved["api_key"] = api_key
            return cfg_path

    # Build the token from parts so the literal never appears contiguously in
    # source (otherwise the repo secret-scan reds the gate on a synthetic fixture).
    _key = "sk-" + "abc123"
    monkeypatch.setattr(cli.ScreenScribeConfig, "load", classmethod(lambda _c: _Cfg()))
    result = runner.invoke(app, ["config", "--set-key", _key])
    assert result.exit_code == 0
    assert saved["api_key"] == _key
    assert "API key saved" in result.output


def test_config_rejects_removed_init_keywords_flag(tmp_path: Path, monkeypatch: Any) -> None:
    """The cwd-writing ``config --init-keywords`` flag is gone.

    Keyword vocabulary now lives in the global ``screenscribe keywords`` group
    (writing ``~/.config/screenscribe/keywords.yaml``), never a per-directory
    ``keywords.yaml``. Analysis must not depend on the terminal's cwd.
    """
    monkeypatch.setattr(
        cli.ScreenScribeConfig, "load", classmethod(lambda _c: ScreenScribeConfig())
    )
    result = runner.invoke(app, ["config", "--init-keywords"])
    assert result.exit_code != 0


def test_config_show_reports_endpoints_and_models(monkeypatch: Any) -> None:
    """config --show surfaces endpoints, models and processing flags."""
    cfg = ScreenScribeConfig(language="pl")
    monkeypatch.setattr("screenscribe.config.ScreenScribeConfig.load", classmethod(lambda _c: cfg))
    result = runner.invoke(app, ["config", "--show"])
    assert result.exit_code == 0
    assert "Endpoints:" in result.output
    assert "Models:" in result.output
    assert "Language: pl" in result.output
