"""Fixtures for the screenscribe review E2E suite.

Discipline: the tests must exercise the INSTALLED artifact, not the source tree
run in-place (the cwd-shadow trap — a stale wheel can be masked by the repo cwd
on sys.path). So we:

1. Build a wheel from current HEAD (``uv build --wheel``).
2. Install it into an ISOLATED venv in a tmp dir (``uv venv`` + ``uv pip install``).
3. Run ``screenscribe`` from a cwd OUTSIDE the repo, using that venv's binary.

Report generation goes through the REAL pipeline once (a short synthetic bug
demo, real STT/LLM/VLM calls), serialized into a session-scoped output dir. The review
server is then started against that pre-generated dir through the SAME serve
code path the user hits (``screenscribe.cli._serve_report`` -> ``tokenized_url``),
so the token-fragment flow is genuinely exercised. We pre-generate once and reuse
to keep the browser assertions deterministic (one pipeline call per session).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FIXTURE_NARRATION = (
    "There is a bug in the dashboard preview. The screen shows a colored test "
    "pattern instead of the dashboard. Please replace the test pattern with "
    "the expected dashboard view."
)

# Tokenized URL printed by the serve path, e.g.
#   http://localhost:8765/fixture_report.html#token=<urlsafe-token>
_URL_RE = re.compile(r"http://localhost:\d+/[^\s]*_report\.html#token=[A-Za-z0-9_\-]+")

# A short, valid 1x1 JPEG (base64, no data: prefix). ~239 chars. Used to mark a
# manual frame. See the length-threshold note in the browser test.
TINY_JPEG_B64 = (  # pragma: allowlist secret
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"  # pragma: allowlist secret
    "AAAAAAAAAAAAAAAAAAAAAv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class InstalledCli:
    """An isolated venv holding the wheel built from HEAD."""

    venv_dir: Path
    python: Path
    binary: Path
    site_packages_pkg: Path  # .../site-packages/screenscribe

    @property
    def review_js(self) -> Path:
        return self.site_packages_pkg / "html_pro_assets" / "scripts" / "review_app.js"


@pytest.fixture(scope="session")
def installed_cli(tmp_path_factory: pytest.TempPathFactory) -> InstalledCli:
    """Build a wheel from HEAD and install it into an isolated venv outside the repo.

    The grep / serve / browser layers all drive THIS binary, never the repo
    source tree, so a stale install cannot hide behind the repo cwd on sys.path.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is required to build/install the wheel for e2e tests")

    work = tmp_path_factory.mktemp("ss_e2e_install")
    dist = work / "dist"

    # 1. Build the wheel from current HEAD into an isolated dist dir.
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted(dist.glob("screenscribe-*.whl"))
    assert wheels, f"no wheel built into {dist}"
    wheel = wheels[-1]

    # 2. Isolated venv + install the wheel.
    venv_dir = work / "venv"
    subprocess.run(["uv", "venv", str(venv_dir)], check=True, capture_output=True, text=True)
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    binary = bin_dir / ("screenscribe.exe" if os.name == "nt" else "screenscribe")
    assert binary.exists(), f"installed binary missing at {binary}"

    # Resolve the installed package from a cwd OUTSIDE the repo: running
    # `import screenscribe` with cwd=REPO_ROOT would let the repo's own
    # `screenscribe/` shadow the wheel on sys.path (the exact cwd-shadow trap).
    pkg_dir_out = subprocess.run(
        [
            str(python),
            "-c",
            "import screenscribe, os; print(os.path.dirname(screenscribe.__file__))",
        ],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    site_pkg = Path(pkg_dir_out)
    assert site_pkg.is_dir(), f"installed package dir missing: {site_pkg}"
    # Guard the cwd-shadow trap: the installed package must NOT be the repo source.
    assert REPO_ROOT not in site_pkg.parents, (
        f"installed package resolves to the repo source tree ({site_pkg}); "
        "the wheel install is being shadowed by the repo cwd"
    )

    return InstalledCli(
        venv_dir=venv_dir,
        python=python,
        binary=binary,
        site_packages_pkg=site_pkg,
    )


def _ffmpeg_bin() -> str | None:
    for cand in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    return None


def _generate_fixture_video(dest: Path) -> None:
    """Render a tiny clip with a video stream + a real SPEECH audio track.

    The pipeline needs a video stream (screenshots) and an audio stream (STT).
    The audio must be actual speech, not a tone: a sine makes STT return nothing
    and is a poor stand-in for the user's runtime. Use an explicit English voice
    with ``say`` or ``espeak``; a missing synthesizer fails the requested gate.
    The narration describes the visible test pattern as a dashboard bug, so all
    analysis stages must execute before browser tests can pass. Generated on demand
    into a tmp dir, never committed: the media guard forbids tracked ``*.mov`` in
    the shippable seed (the seed ships from ``git archive HEAD``).
    """
    ffmpeg = _ffmpeg_bin()
    if ffmpeg is None:
        pytest.fail("ffmpeg is required for the requested pipeline-backed E2E gate")
    dest.parent.mkdir(parents=True, exist_ok=True)

    say = shutil.which("say")
    if say is not None:
        speech = dest.parent / "speech.aiff"
        subprocess.run(
            [
                say,
                "-v",
                "Samantha",
                "-r",
                "150",
                "-o",
                str(speech),
                FIXTURE_NARRATION,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if espeak is None:
            pytest.fail("say or espeak-ng/espeak is required for the speech E2E fixture")
        speech = dest.parent / "speech.wav"
        subprocess.run(
            [espeak, "-v", "en-us", "-s", "150", "-w", str(speech), FIXTURE_NARRATION],
            check=True,
            capture_output=True,
            text=True,
        )

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10",
            "-i",
            str(speech),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def fixture_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Tiny speech+video fixture generated into a session tmp dir (no repo clutter)."""
    dest = tmp_path_factory.mktemp("ss_e2e_fixture") / "fixture.mov"
    _generate_fixture_video(dest)
    assert dest.exists(), f"fixture video could not be generated: {dest}"
    return dest


def _assert_generated_review(report_json: Path) -> None:
    """Reject a green browser run over an empty or degraded pipeline result."""
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report.get("transcript_source") == "audio", "E2E must exercise real STT"
    transcript = report.get("transcript", "").lower()
    assert len(transcript.split()) >= 15 and all(
        word in transcript for word in ("dashboard", "test", "pattern", "replace")
    ), "STT did not recover the synthetic bug narration; inspect the pipeline before browser tests"
    assert report.get("transcript_segments"), "STT produced no transcript segments"
    assert not report.get("errors"), "Pipeline reported errors"
    passes = report.get("analysis_passes", {})
    for stage in ("detections", "screenshots", "unified_analysis"):
        result = passes.get(stage, {})
        assert result.get("status") == "completed" and result.get("count", 0) > 0, (
            f"Pipeline stage {stage} did not complete with evidence"
        )
    assert report.get("findings"), "The narrated dashboard bug produced no findings"
    for finding in report["findings"]:
        analysis = finding.get("unified_analysis", {})
        assert analysis.get("status") == "completed" and not analysis.get("degraded", True), (
            "VLM analysis is missing or degraded"
        )
        screenshot = finding.get("screenshot")
        assert screenshot, "Finding has no screenshot"
        path = report_json.parent / "screenshots" / screenshot
        assert path.is_file() and path.stat().st_size > 0, "Screenshot artifact is missing or empty"
    assert any(f["unified_analysis"].get("is_issue") for f in report["findings"]), (
        "VLM did not identify the narrated dashboard bug"
    )


@pytest.fixture(scope="session")
def generated_review(
    installed_cli: InstalledCli,
    fixture_video: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Run the real pipeline ONCE via the installed binary, cwd OUTSIDE the repo.

    Returns the output dir (``<stem>_review/``) holding the generated
    ``<stem>_report.html`` + ``.json``. Reused across browser assertions so the
    pipeline runs only once per session.
    """
    work = tmp_path_factory.mktemp("ss_e2e_run")  # cwd is OUTSIDE the repo
    video_copy = work / fixture_video.name
    shutil.copy2(fixture_video, video_copy)

    env = dict(os.environ)
    env["BROWSER"] = "true"  # no-op any webbrowser.open

    # Include semantic detection, per-frame VLM and summary calls, not just
    # STT + rendering. The old five-minute bound killed a live run mid-analysis.
    # Keep partial output privately for diagnosis if a provider exceeds the bound.
    pipeline_log = work / "pipeline.log"
    with pipeline_log.open("w", encoding="utf-8") as output:
        pipeline_log.chmod(0o600)
        try:
            proc = subprocess.run(
                [
                    str(installed_cli.binary),
                    "review",
                    video_copy.name,
                    "--lang",
                    "en",
                    "--no-serve",
                ],
                cwd=work,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(f"Pipeline exceeded 900 seconds; inspect private log: {pipeline_log}")
    pipeline_output = pipeline_log.read_text(encoding="utf-8")
    if proc.returncode != 0:
        combined = pipeline_output
        # --run-e2e is the explicit "run the REAL gate" request: reaching this
        # fixture means the caller opted into the pipeline-backed e2e suite. A
        # missing API key here used to `pytest.skip(...)`, which silently masked
        # ~13/23 tests — a keyless CI then reported the whole e2e job "green"
        # while half of it never ran (macro-theater). So a missing key is now a
        # HARD FAILURE, mirroring the `chromium` fixture's discipline: running the
        # gate without its prerequisites must be LOUD, never green-by-omission.
        # The operator (and any local run) supplies SCREENSCRIBE_API_KEY, so the
        # pipeline succeeds and this branch is never hit; keyless CI goes RED on
        # purpose until a key is provided (or e2e is left opt-out, the default).
        if "No API key configured" in combined or "API Key Error" in combined:
            pytest.fail(
                "--run-e2e was requested but no API key is configured, so the "
                "pipeline-backed e2e tests cannot run. A silent skip here would "
                "let keyless CI report a false green (half the suite unrun). Set "
                "SCREENSCRIBE_API_KEY to run the gate, or drop --run-e2e to opt "
                "out of e2e entirely.\n"
                f"Inspect private log: {pipeline_log}"
            )
        pytest.fail(
            "pipeline generation failed (network/key required for STT/LLM/VLM?):\n"
            f"Inspect private log: {pipeline_log}"
        )

    out_dir = work / f"{video_copy.stem}_review"
    report_html = out_dir / f"{video_copy.stem}_report.html"
    report_json = out_dir / f"{video_copy.stem}_report.json"
    assert report_html.exists(), f"report HTML not generated: {report_html}; log: {pipeline_log}"
    assert report_json.exists(), f"report JSON not generated: {report_json}"
    _assert_generated_review(report_json)
    return out_dir


@dataclass
class ReviewServer:
    url: str  # http://localhost:<port>/<stem>_report.html#token=<token>
    port: int
    token: str
    output_dir: Path


# The CLI's own serve path prints the tokenized URL through Rich, which
# line-wraps long URLs and splits the token across lines — unparseable from
# captured stdout. This launcher drives the SAME production code path
# (create_review_app -> install_security token -> tokenized_url -> uvicorn) but
# prints the URL with a plain, unwrapped sentinel line we can parse reliably.
_SERVE_LAUNCHER = r"""
import sys
from pathlib import Path

import uvicorn

from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app
from screenscribe.server_security import tokenized_url

output_dir = Path(sys.argv[1])
video_path = Path(sys.argv[2])
port = int(sys.argv[3])
report_filename = sys.argv[4]

config = ScreenScribeConfig.load()
app = create_review_app(
    output_dir=output_dir,
    report_filename=report_filename,
    video_path=video_path,
    config=config,
)
url = tokenized_url(
    f"http://localhost:{port}/{report_filename}", app.state.session_token
)
# Plain, single-line, unwrapped — robust to parse from captured stdout.
print("E2E_REVIEW_URL " + url, flush=True)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
"""


@pytest.fixture
def review_server(
    installed_cli: InstalledCli,
    generated_review: Path,
    fixture_video: Path,
) -> Iterator[ReviewServer]:
    """Start the review server (installed binary, real serve path) and parse its
    tokenized URL from stdout. Tears the subprocess down at test end."""
    port = _free_port()
    report_html = next(generated_review.glob("*_report.html"))
    report_filename = report_html.name
    # Keep a real copy of the source video next to the report so the serve
    # path's video endpoints resolve.
    video_path = generated_review / fixture_video.name
    if not video_path.exists():
        shutil.copy2(fixture_video, video_path)

    env = dict(os.environ)
    env["BROWSER"] = "true"  # no-op any browser open

    proc = subprocess.Popen(
        [
            str(installed_cli.python),
            "-c",
            _SERVE_LAUNCHER,
            str(generated_review),
            str(video_path),
            str(port),
            report_filename,
        ],
        cwd=generated_review.parent,  # OUTSIDE the repo (tmp)
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    url = None
    captured: list[str] = []
    deadline = time.time() + 60
    try:
        assert proc.stdout is not None
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            captured.append(line)
            if line.startswith("E2E_REVIEW_URL "):
                url = line[len("E2E_REVIEW_URL ") :].strip()
                break
        if url is None or not _URL_RE.match(url):
            proc.terminate()
            raise AssertionError(
                f"review server did not print a parseable tokenized URL (got {url!r}).\n"
                + "".join(captured)
            )

        port_from_url = int(re.search(r"localhost:(\d+)/", url).group(1))
        token = url.split("#token=", 1)[1]

        # Poll until the report HTML actually serves (uvicorn warmup).
        _wait_http_ok(f"http://localhost:{port_from_url}/{report_filename}", deadline=deadline)

        yield ReviewServer(url=url, port=port_from_url, token=token, output_dir=generated_review)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _wait_http_ok(probe_url: str, deadline: float) -> None:
    import urllib.error
    import urllib.request

    # probe_url points at the output-dir static mount root; we only need the
    # server to answer, so accept any HTTP status (not a connection refusal).
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(probe_url, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # server is up, just a non-200 path
        except Exception as exc:  # ConnectionRefused etc.
            last_err = exc
            time.sleep(0.25)
    raise AssertionError(f"review server never became reachable: {last_err}")


@pytest.fixture(scope="session")
def chromium():
    """Session-scoped Playwright Chromium (cached headless shell — no download).

    STRICT: e2e only runs under --run-e2e, and the whole point of this gate is to
    drive a REAL browser. So a missing Playwright package or a browser that won't
    launch is a hard FAILURE here, never a silent skip — accidentally running the
    gate without a browser must be loud, not green-by-omission.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # package not installed
        pytest.fail(
            "--run-e2e requires Playwright but it is not importable "
            f"({exc}). Install it: `uv add --group dev playwright` "
            "(browsers are already cached under ~/Library/Caches/ms-playwright)."
        )

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # browser missing / won't launch
        if pw is not None:
            pw.stop()
        pytest.fail(
            f"--run-e2e requires a launchable Playwright Chromium but it failed ({exc}). "
            "Browsers should be cached under ~/Library/Caches/ms-playwright."
        )

    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


@pytest.fixture
def browser_context(chromium):
    """A fresh browser context per test (accepts downloads for ZIP export)."""
    context = chromium.new_context(accept_downloads=True)
    try:
        yield context
    finally:
        context.close()
