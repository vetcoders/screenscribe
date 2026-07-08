"""CLI interface for screenscribe video review automation."""

import os
import shutil
import socket
import subprocess
import sys
import webbrowser as webbrowser
from pathlib import Path
from typing import Annotated

import typer
import typer.rich_utils as _typer_rich_utils
from rich.console import Console
from rich.panel import Panel

from . import __version__
from .audio import (
    FFmpegNotFoundError,
    check_ffmpeg_installed,
)

# Re-exported so the CLI namespace stays the patch surface for the audio guards
# in cli_messages and the review pipeline (tests patch
# screenscribe.cli.extract_audio / .require_audio_stream / .get_video_duration;
# the callers read them via cli.<name>).
from .audio import extract_audio as extract_audio
from .audio import get_video_duration as get_video_duration
from .audio import require_audio_stream as require_audio_stream
from .audio import tail_is_silent as tail_is_silent
from .cli_estimate import (
    ESTIMATE_SEMANTIC_PER_DETECTION as ESTIMATE_SEMANTIC_PER_DETECTION,
)
from .cli_estimate import (
    ESTIMATE_SEMANTIC_PREFILTER_PER_MINUTE as ESTIMATE_SEMANTIC_PREFILTER_PER_MINUTE,
)
from .cli_estimate import (
    ESTIMATE_STT_PER_MINUTE as ESTIMATE_STT_PER_MINUTE,
)
from .cli_estimate import (
    ESTIMATE_UNIFIED_PER_DETECTION as ESTIMATE_UNIFIED_PER_DETECTION,
)
from .cli_estimate import (
    ESTIMATE_VISION_PER_DETECTION as ESTIMATE_VISION_PER_DETECTION,
)
from .cli_estimate import (
    _show_estimate as _show_estimate,
)
from .cli_messages import (
    _build_transcript_timeline_coverage_message as _build_transcript_timeline_coverage_message,
)
from .cli_messages import (
    _build_transcription_failure_message as _build_transcription_failure_message,
)
from .cli_messages import (
    _extract_audio_or_exit as _extract_audio_or_exit,
)
from .cli_messages import (
    _require_audio_or_exit as _require_audio_or_exit,
)
from .cli_messages import (
    _transcribe_audio_or_exit as _transcribe_audio_or_exit,
)
from .cli_paths import (
    MAX_REVIEW_VERSIONS as MAX_REVIEW_VERSIONS,
)
from .cli_paths import (
    _find_next_review_path as _find_next_review_path,
)
from .cli_paths import (
    _find_next_versioned_path as _find_next_versioned_path,
)
from .cli_reporting import (
    _print_report_artifact_paths as _print_report_artifact_paths,
)
from .cli_reporting import (
    _write_report_artifacts as _write_report_artifacts,
)
from .cli_serve import (
    _serve_report as _serve_report,
)
from .config import ScreenScribeConfig
from .keywords import (
    CATEGORIES,
    GLOBAL_KEYWORDS_PATH,
    KeywordsConfig,
    save_default_keywords,
)
from .preprocess import write_preprocess_bundle

# Explicit re-exports: review_pipeline calls these through the cli module
# (cli.print_report / cli.save_enhanced_json_report) so they stay the patch
# surface; the pre-AI basic-json snapshot also routes through here.
from .report import print_report as print_report
from .report import save_enhanced_json_report as save_enhanced_json_report

# Explicit re-export: review_pipeline calls cli.semantic_prefilter and tests
# patch screenscribe.cli.semantic_prefilter.
from .semantic_filter import semantic_prefilter as semantic_prefilter

# Explicit re-export: the audio/STT guards in cli_messages and (next) the
# review pipeline read transcribe_audio via cli.transcribe_audio, and tests
# patch screenscribe.cli.transcribe_audio.
from .transcribe import filter_hallucinated_segments as filter_hallucinated_segments
from .transcribe import transcribe_audio as transcribe_audio
from .transcribe import transcribe_audio_chunked as transcribe_audio_chunked
from .validation import APIKeyError, ModelValidationError, validate_models

# Legacy imports kept for backwards compatibility (not used in unified pipeline)

# Typer's rich help/usage renderer forces a terminal (ANSI escapes + box
# borders) whenever GITHUB_ACTIONS / FORCE_COLOR / PY_COLORS is set, regardless
# of whether stdout is an actual TTY. That leaks control codes into piped output
# (e.g. `screenscribe --help | less`) and breaks help/usage assertions in CI.
# Drop the implicit GITHUB_ACTIONS forcing and let rich auto-detect via isatty(),
# so help stays colored in a real terminal but renders plain when piped or in CI.
# Explicit FORCE_COLOR / PY_COLORS opt-ins are still honored.
_typer_rich_utils.FORCE_TERMINAL = (
    True if (os.getenv("FORCE_COLOR") or os.getenv("PY_COLORS")) else None
)

console = Console()

BOOTSTRAP_BANNER_SHOWN_ENV = "SCREENSCRIBE_BOOTSTRAP_BANNER_SHOWN"


def _version_string() -> str:
    """Version string for explicit --version / version queries.

    Appends short git provenance (``+g<sha>``, plus ``-dirty`` for an unclean
    tree) when the package runs from a source / editable checkout, so a dev
    build is never mistaken for a release. A wheel / PyPI install has no git
    work tree, so it shows the clean ``v<version>``.
    """
    base = f"v{__version__}"
    git = shutil.which("git")
    if git is None:
        return base
    pkg_dir = Path(__file__).resolve().parent
    try:
        head = subprocess.run(
            [git, "-C", str(pkg_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return base
    if not head:
        return base
    dirty = subprocess.run(
        [git, "-C", str(pkg_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout.strip()
    return f"{base}+g{head}{'-dirty' if dirty else ''}"


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        console.print(f"[bold]Screenscribe[/] {_version_string()}")
        raise typer.Exit()


app = typer.Typer(
    name="screenscribe",
    help="Turn screen recordings into actionable engineering reports. STT→LLM→VLM pipeline.",
    add_completion=False,
    invoke_without_command=True,
)


def _is_video_file(path: str) -> bool:
    """Check if path looks like a video file."""
    video_extensions = {".mov", ".mp4", ".avi", ".mkv", ".webm", ".m4v", ".wmv"}
    return Path(path).suffix.lower() in video_extensions


def _auto_review_if_video() -> None:
    """If first positional arg is a video file, inject 'review' command."""
    if len(sys.argv) > 1:
        first_arg = sys.argv[1]
        # Skip if it's already a command or flag
        if first_arg in (
            "review",
            "analyze",
            "transcribe",
            "preprocess",
            "config",
            "version",
            "--help",
            "-h",
            "--version",
            "-V",
            "--config",
        ):
            return
        # Check if it looks like a video file (has video extension)
        if _is_video_file(first_arg):
            # Inject 'review' command
            sys.argv.insert(1, "review")


# Auto-detect video files and inject review command
_auto_review_if_video()


def _interactive_mode() -> None:
    """Launch interactive mode when no subcommand is given."""
    from rich.prompt import Prompt

    console.print()
    console.print(
        Panel(
            f"[bold]Screenscribe[/] v{__version__}\n"
            "[dim]Turn screen recordings into engineering reports[/]",
            border_style="green",
        )
    )
    console.print()

    # Command selection
    commands = {
        "1": ("review", "Analyze video and generate report"),
        "2": ("analyze", "Live workbench - mark moments while watching"),
        "3": ("preprocess", "Transcript-first artifact bundle"),
        "4": ("transcribe", "Transcribe video only"),
        "5": ("keywords", "Show/edit AI detection keywords"),
        "6": ("config", "Show/edit configuration"),
        "7": ("version", "Show version info"),
    }

    console.print("[bold]Select command:[/]")
    for key, (cmd, desc) in commands.items():
        console.print(f"  [cyan]{key}[/]) [bold]{cmd}[/] - {desc}")
    console.print()

    choice = Prompt.ask("Enter choice", choices=["1", "2", "3", "4", "5", "6", "7"], default="1")
    selected_cmd = commands[choice][0]

    if selected_cmd == "version":
        console.print(f"\n[bold]Screenscribe[/] v{__version__}")
        raise typer.Exit()

    if selected_cmd == "config":
        console.print("\n[dim]Running:[/] uv run screenscribe config --show")
        subprocess.run([sys.executable, "-m", "screenscribe", "config", "--show"])
        raise typer.Exit()

    if selected_cmd == "keywords":
        console.print("\n[dim]Running:[/] screenscribe keywords list")
        subprocess.run([sys.executable, "-m", "screenscribe", "keywords", "list"])
        raise typer.Exit()

    # For review/transcribe, ask for video path
    console.print()
    video_path = Prompt.ask(
        "[bold]Video path[/] (paste or drag file here)",
        default="",
    )

    if not video_path.strip():
        console.print("[red]No video path provided. Exiting.[/]")
        raise typer.Exit(1)

    # Clean path (remove quotes if dragged)
    video_path = video_path.strip().strip("'\"")
    video = Path(video_path)

    if not video.exists():
        console.print(f"[red]File not found:[/] [link=file://{video}]{video}[/link]")
        raise typer.Exit(1)

    console.print()
    console.print(f"[dim]Running:[/] screenscribe {selected_cmd} {video}")
    console.print()

    # Use subprocess to call commands (avoids forward reference issues)
    run_cmd = [sys.executable, "-m", "screenscribe", selected_cmd, str(video)]
    subprocess.run(run_cmd)


def _resolve_editor() -> str:
    """Return the editor command to open a file with.

    Honors ``$EDITOR``; otherwise falls back to ``open`` on macOS (delegates to
    the OS default app) and ``nano`` elsewhere. This is the single editor
    resolution used by both ``--config`` and the ``keywords edit`` command.
    """
    return os.environ.get("EDITOR", "open" if sys.platform == "darwin" else "nano")


def _open_in_editor(path: Path, label: str) -> None:
    """Open ``path`` in the resolved editor, printing a short status line."""
    editor = _resolve_editor()
    console.print(f"[dim]Opening {label} in {editor}...[/]")
    subprocess.run([editor, str(path)])


def _open_config_callback(value: bool) -> None:
    """Open config file in editor."""
    if value:
        config_path = Path.home() / ".config" / "screenscribe" / "config.env"
        if not config_path.exists():
            # Create default config
            cfg = ScreenScribeConfig.load()
            cfg.save_default_config()
            console.print(f"[green]Created default config:[/] {config_path}")

        _open_in_editor(config_path, "config")
        raise typer.Exit()


def _check_ffmpeg_or_exit() -> None:
    """Verify FFmpeg/FFprobe are installed, exiting cleanly if not.

    Single shared guard for the ``review``/``transcribe``/``preprocess`` paths
    so a missing toolchain yields one identical, traceback-free error instead of
    a raw ``FFmpegNotFoundError`` stack trace.
    """
    try:
        check_ffmpeg_installed()
    except FFmpegNotFoundError as e:
        console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(1) from None


def _find_available_port(preferred_port: int, max_tries: int = 25) -> int:
    """Return ``preferred_port`` if free, else the next bindable port.

    Probes by binding ``127.0.0.1`` (the same host uvicorn binds) so a port held
    by another process is detected via ``OSError`` and skipped. Falls back to the
    preferred port if no candidate is free within ``max_tries`` (uvicorn then
    surfaces its own error rather than us guessing further).
    """
    candidate = preferred_port
    for _ in range(max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                candidate += 1
    return preferred_port


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
    config: Annotated[
        bool,
        typer.Option(
            "--config",
            callback=_open_config_callback,
            is_eager=True,
            help="Open config file in editor (creates default if missing).",
        ),
    ] = False,
) -> None:
    """Screenscribe - Turn screen recordings into actionable engineering reports. STT→LLM→VLM pipeline."""
    # If no subcommand given, launch interactive mode — but only on a real TTY.
    # Under a pipe/CI (non-interactive stdin) the Prompt.ask calls would raise a
    # bare EOFError; show help and exit cleanly instead.
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty():
            console.print(
                "[yellow]Interactive mode requires a terminal.[/] "
                "Run a subcommand instead, e.g. [bold]screenscribe review <video>[/]."
            )
            console.print(ctx.get_help())
            raise typer.Exit(2)
        _interactive_mode()


@app.command()
def review(
    videos: Annotated[
        list[Path],
        typer.Argument(
            help="Path(s) to video file(s) - multiple files processed with shared context",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output directory for screenshots and reports",
        ),
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            "-P",
            help="Append custom instructions to semantic, semantic prefilter, and vision prompts",
        ),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--lang",
            "-l",
            help="Language code for transcription (defaults to config)",
        ),
    ] = None,
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Use local STT server instead of LibraxisAI cloud",
        ),
    ] = False,
    vision: Annotated[
        bool,
        typer.Option(
            "--vision/--no-vision",
            # Hidden power-user alias: a second flag pair whose disable name is
            # ``--no-vlm`` (the enable side is an empty/space token, so it is not a
            # second visible "on" name and does not duplicate ``--vision``).
            " /--no-vlm",
            help="Skip visual/screenshot analysis. Semantic LLM detection still runs.",
        ),
    ] = True,
    json_report: Annotated[
        bool,
        typer.Option(
            "--json/--no-json",
            help="Save JSON report",
        ),
    ] = True,
    markdown_report: Annotated[
        bool,
        typer.Option(
            "--markdown/--no-markdown",
            "--md",
            help="Save Markdown report",
        ),
    ] = True,
    html_report: Annotated[
        bool,
        typer.Option(
            "--html/--no-html",
            help="Save interactive HTML report with human review workflow",
        ),
    ] = True,
    embed_video: Annotated[
        bool,
        typer.Option(
            "--embed-video",
            help="Embed video as base64 in the HTML report (only for files <50MB)",
        ),
    ] = False,
    keywords_file: Annotated[
        Path | None,
        typer.Option(
            "--keywords-file",
            "-k",
            help=(
                "Path to a custom keywords YAML file. Keywords are passed to the AI "
                "as hints during detection; they do not replace LLM "
                "analysis and an empty/absent file is safe."
            ),
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Resume from previous checkpoint if available",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Force reprocessing, ignore existing checkpoint",
        ),
    ] = False,
    estimate: Annotated[
        bool,
        typer.Option(
            "--estimate",
            help="Show time estimate without processing (uses video duration)",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Preview detections without generating reports. NOTE: this still "
                "runs paid transcription (STT) + LLM detection; for a zero-cost "
                "preview use --estimate instead."
            ),
        ),
    ] = False,
    skip_validation: Annotated[
        bool,
        typer.Option(
            "--skip-validation",
            help="Skip model availability check (faster start, may fail mid-pipeline)",
        ),
    ] = False,
    serve: Annotated[
        bool,
        typer.Option(
            "--serve/--no-serve",
            help="Start HTTP server and open report in browser after processing",
        ),
    ] = True,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port for the HTTP server (default: 8765)",
        ),
    ] = 8765,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed progress and debug information",
        ),
    ] = False,
) -> None:
    """
    Analyze screencast video(s) and generate interactive review reports.

    Pipeline: Audio → STT → Semantic Analysis → Screenshots → VLM → Report

    Features:
    • Response ID chaining: STT→LLM→VLM share context for better analysis
    • Auto-versioning: existing reviews preserved as video_review_2, _3, etc.
    • Interactive HTML report with video player, subtitle sync, and annotations
    • Batch mode: multiple videos with shared context across files

    Detection:
    • Semantic pre-filter: the LLM analyzes the entire transcript.
    • Keywords: your phrase dictionary (the global
      ~/.config/screenscribe/keywords.yaml) is passed to the AI as hints during
      detection, used by default if present (always on, safe when empty). They
      never replace the LLM analysis. Manage them with `screenscribe keywords`.
    • --keywords-file: override the global dictionary with a per-run file.

    Output options:
    • --serve/--no-serve: Start HTTP server and open report in browser
    • --force: Overwrite existing review instead of versioning
    • --resume: Continue from checkpoint if interrupted

    Examples:
        uv run screenscribe review video.mov
        uv run screenscribe review video1.mov video2.mov video3.mov
        uv run screenscribe review ./recordings/*.mov --no-serve
        uv run screenscribe review video.mov --keywords-file my-keywords.yaml
    """
    # Validate video paths exist
    for video in videos:
        if not video.exists():
            console.print(f"[red]Error:[/] Video not found: [link=file://{video}]{video}[/link]")
            raise typer.Exit(1)
        if video.is_dir():
            console.print(
                f"[red]Error:[/] Path is a directory: [link=file://{video}]{video}[/link]"
            )
            raise typer.Exit(1)

    # Bootstrap prints an immediate banner before heavy imports; avoid duplicate header.
    if os.environ.get(BOOTSTRAP_BANNER_SHOWN_ENV) != "1":
        console.print(
            Panel(
                f"[bold cyan]Screenscribe v{__version__}[/]\n"
                "[dim]Turn screen recordings into engineering reports[/]",
                border_style="cyan",
            )
        )

    # Check FFmpeg is installed (shared guard — identical message across commands)
    _check_ffmpeg_or_exit()

    # --estimate is a zero-cost preview that only needs the container duration,
    # not a decoded audio stream. Requiring audio here would exit an estimate on
    # an audioless clip; skip the audio guard in estimate mode (run_review reads
    # the duration defensively and still renders the table).
    if not estimate:
        for video in videos:
            _require_audio_or_exit(video)

    # --embed-video inlines the clip as base64, but the HTML renderer silently
    # falls back to a file reference for anything >=50MB. Warn up front so the
    # degradation is not a surprise (the report would otherwise just not embed).
    if embed_video:
        embed_video_max_bytes = 50 * 1024 * 1024
        for video in videos:
            try:
                size_bytes = video.stat().st_size
            except OSError:
                continue
            if size_bytes >= embed_video_max_bytes:
                size_mb = size_bytes / (1024 * 1024)
                console.print(
                    f"[yellow]Warning:[/] {video.name} is ~{size_mb:.0f}MB (>=50MB); "
                    "--embed-video will fall back to a file reference instead of "
                    "embedding the video in the HTML report."
                )

    # Load configuration
    config = ScreenScribeConfig.load()
    if language is not None:
        config.language = language
    language = config.language
    config.use_vision_analysis = vision
    config.verbose = verbose
    config.analysis_prompt_override = (prompt or "").strip()

    # Non-blocking key<->endpoint mismatch warnings (sk- gateways are legit, so
    # a prefix mismatch is surfaced, not blocked).
    for warning in config.mismatch_warnings():
        console.print(f"[yellow]Config Warning:[/] {warning}")

    # Validate endpoint configuration (fail fast on genuinely fatal mistakes)
    config_errors = config.validate()
    if config_errors:
        for error in config_errors:
            console.print(f"[red]Config Error:[/] {error}")
        raise typer.Exit(1)

    # Validate model availability (fail fast). --local only reroutes STT to a
    # LOCAL Whisper server; the LLM pre-filter and the Vision stage still hit the
    # cloud, so they must be validated even under --local -- only the STT probe is
    # skipped. --estimate is a zero-cost preview and skips validation entirely.
    if not skip_validation and not estimate:
        try:
            validate_models(config, use_vision=vision, validate_stt=not local)
        except APIKeyError as e:
            console.print(f"[red]API Key Error:[/] {e}")
            raise typer.Exit(1) from None
        except ModelValidationError as e:
            console.print(f"[red]Model Error:[/] {e}")
            console.print(
                f"[dim]Tip: Check SCREENSCRIBE_{e.model_type.upper()}_MODEL in "
                "~/.config/screenscribe/config.env[/]"
            )
            raise typer.Exit(1) from None

    # Delegate the per-video stage orchestration (audio -> transcribe ->
    # detection -> screenshots -> unified VLM -> report, checkpointing,
    # empty-state / dry-run, batch chaining, optional local serve) to the
    # review pipeline. review_pipeline calls every patchable step through
    # this module so the monkeypatch surface is preserved.
    from . import review_pipeline

    # Load the active keyword vocabulary once (explicit --keywords-file, else
    # global user file, else built-in default). These are passed to the AI as
    # hints during detection; an empty/absent dictionary is a safe no-op.
    keywords = KeywordsConfig.load(keywords_file)

    # C6.3: --dry-run does NOT mean zero-cost. Despite the name, it still runs
    # Step 2 transcription (paid STT unless --local) and Step 3 issue detection
    # (the LLM semantic prefilter, ALWAYS paid) before exiting -- it only skips
    # report artifacts. Warn before the first paid call so the user can abort.
    # (--estimate is the real zero-cost path and exits before any paid call.)
    if dry_run and not estimate:
        if local:
            cost_line = (
                "Transcription runs locally (--local, no STT cost), but issue "
                "detection still calls the LLM and incurs API cost."
            )
        else:
            cost_line = (
                "It still runs paid transcription (STT) and LLM issue detection before exiting."
            )
        console.print(
            Panel(
                f"--dry-run is NOT free. {cost_line}\n\n"
                "[dim]For a zero-cost preview (no STT, no LLM), use --estimate "
                "instead. Press Ctrl-C now to abort.[/]",
                title="[bold yellow]Dry-run incurs API cost[/]",
                border_style="yellow",
            )
        )

    review_pipeline.run_review(
        videos,
        config,
        output=output,
        language=language,
        local=local,
        vision=vision,
        json_report=json_report,
        markdown_report=markdown_report,
        html_report=html_report,
        embed_video=embed_video,
        keywords=keywords,
        resume=resume,
        force=force,
        estimate=estimate,
        dry_run=dry_run,
        serve=serve,
        port=port,
    )


@app.command()
def analyze(
    video: Annotated[
        Path,
        typer.Argument(
            help="Path to video file for interactive analysis",
            exists=True,
            dir_okay=False,
        ),
    ],
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port for the analysis server",
        ),
    ] = 8766,
    language: Annotated[
        str | None,
        typer.Option(
            "--lang",
            "-l",
            help=(
                "Language code for voice transcription and default dashboard language "
                "(defaults to config; PL/EN toggle controls the UI and future VLM analyses)"
            ),
        ),
    ] = None,
    keywords_file: Annotated[
        Path | None,
        typer.Option(
            "--keywords-file",
            "-k",
            help=(
                "Path to a custom keywords YAML file. Keywords are passed to the AI "
                "as hints when interpreting your marker comments; they do "
                "not replace LLM analysis and an empty/absent file is safe."
            ),
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """
    Start the interactive, human-first analysis dashboard.

    Opens a browser with video player where you can:
    - Watch the video and pause at interesting moments
    - Record voice comments describing issues
    - Mark frames for AI analysis
    - Get real-time VLM analysis results

    This is the "human-first" mode: you guide the AI by pointing
    out what matters, instead of letting AI process everything blindly.

    The dashboard language defaults to the configured screenscribe language
    (English out of the box). --lang overrides it for voice transcription and
    the default dashboard language. The PL/EN toggle controls the UI and
    future VLM analyses for newly analyzed frames.

    Keywords:
    • Your keywords (the global ~/.config/screenscribe/keywords.yaml, used by
      default if present) are passed to the AI as hints when it interprets your
      marker comment + voice note (always on, safe when empty); they never
      replace the LLM and do not auto-create findings. Manage them with
      `screenscribe keywords`.
    • --keywords-file: override the global dictionary with a per-run file.

    Examples:
        uv run screenscribe analyze video.mov
        uv run screenscribe analyze video.mov --port 9000
        uv run screenscribe analyze video.mov --lang pl
        uv run screenscribe analyze video.mov --keywords-file my-keywords.yaml
    """
    import uvicorn

    from .analyze_server import create_analyze_app
    from .server_security import tokenized_url

    config = ScreenScribeConfig.load()
    if language is not None:
        config.language = language

    # Endpoint configuration checks — mirror the `review` command, scoped to the
    # providers `analyze` actually uses: vision (frame analysis) + STT (voice
    # notes); it never contacts the LLM, so a stale/unused LLM mismatch must not
    # be considered (finding I). A key<->endpoint mismatch (e.g. an sk- key on a
    # non-OpenAI endpoint) is a NON-blocking warning -- OpenAI-compatible
    # gateways legitimately use sk- keys (finding 283).
    for warning in config.mismatch_warnings(providers={"vision", "stt"}):
        console.print(f"[yellow]Config Warning:[/] {warning}")

    config_errors = config.validate(providers={"vision", "stt"})
    if config_errors:
        for error in config_errors:
            console.print(f"[red]Config Error:[/] {error}")
        raise typer.Exit(1)

    # Load the active keyword vocabulary (explicit --keywords-file, else global
    # user file, else built-in default) and attach it to the config that backs
    # the live analyze server's session. This is how the active dictionary reaches
    # the per-marker analysis prompt — not only the CLI review path. An empty or
    # absent dictionary is a safe no-op.
    config.keywords = KeywordsConfig.load(keywords_file)

    # Validate API key
    if not config.get_vision_api_key():
        console.print("[red]Error:[/] API key required for VLM analysis")
        console.print(
            "[dim]Set SCREENSCRIBE_API_KEY or run: uv run screenscribe config --set-key YOUR_KEY[/]"
        )
        raise typer.Exit(1)

    # Validate model availability up front — mirrors `review` so a dead key or an
    # unreachable model fails at pre-flight with a clear message instead of deep
    # inside the first frame analysis. analyze uses Vision (frame analysis) + STT
    # (voice notes) and never the LLM, so the LLM probe is skipped.
    try:
        validate_models(config, use_vision=True, validate_stt=True, validate_llm=False)
    except APIKeyError as e:
        console.print(f"[red]API Key Error:[/] {e}")
        raise typer.Exit(1) from None
    except ModelValidationError as e:
        console.print(f"[red]Model Error:[/] {e}")
        console.print(
            f"[dim]Tip: Check SCREENSCRIBE_{e.model_type.upper()}_MODEL in "
            "~/.config/screenscribe/config.env[/]"
        )
        raise typer.Exit(1) from None

    # Pick a free port (bind-probe on 127.0.0.1, same host uvicorn binds) so a
    # busy default no longer crashes with a raw OSError. The resolved port flows
    # into the URL, panel and uvicorn.run together — never out of sync.
    resolved_port = _find_available_port(port)
    if resolved_port != port:
        console.print(
            f"[yellow]Port {port} is busy, using {resolved_port} for this analyze server.[/]"
        )
    port = resolved_port

    app_instance = create_analyze_app(video.resolve(), config)
    # /api/* is gated by a one-time session token carried in the URL fragment
    # (#token=...); the UI must be opened at exactly this URL.
    url = tokenized_url(f"http://localhost:{port}", app_instance.state.session_token)

    console.print()
    console.print(
        Panel(
            f"[bold cyan]Screenscribe Analyze[/]\n"
            f"[dim]Interactive video analysis - human-first mode[/]\n\n"
            f"Video: [link=file://{video.resolve()}]{video.name}[/link]\n"
            f"Server: [bold]http://localhost:{port}[/]",
            border_style="cyan",
        )
    )

    # Open browser with the tokenized URL
    webbrowser.open(url)
    console.print(f"[bold]Open:[/] {url}")
    console.print()
    console.print("[dim]Press Ctrl+C to stop the server and exit[/]")
    console.print()

    # Start server
    try:
        uvicorn.run(app_instance, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped.[/]")


@app.command()
def transcribe(
    video: Annotated[
        Path,
        typer.Argument(
            help="Path to video file",
            exists=True,
            dir_okay=False,
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output file for transcript",
        ),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--lang",
            "-l",
            help="Language code for transcription (defaults to config)",
        ),
    ] = None,
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Use local STT server",
        ),
    ] = False,
) -> None:
    """
    Transcribe video audio to text (no analysis).

    Quick transcription using LibraxisAI STT or local Whisper.
    Outputs plain text transcript to stdout or file.

    Examples:
        uv run screenscribe transcribe video.mov
        uv run screenscribe transcribe video.mov -o transcript.txt
        uv run screenscribe transcribe video.mov --local --lang en
    """
    config = ScreenScribeConfig.load()
    if language is not None:
        config.language = language
    language = config.language

    # Check FFmpeg is installed (shared guard — identical message across commands)
    _check_ffmpeg_or_exit()

    # Extract audio
    audio_path = _extract_audio_or_exit(video)

    # Transcribe
    result = _transcribe_audio_or_exit(
        audio_path,
        language=language,
        use_local=local,
        api_key=config.get_stt_api_key(),
        stt_endpoint=config.stt_endpoint,
        stt_model=config.stt_model,
    )

    # Drop no-speech hallucinations (outros Whisper invents on music / silence)
    # before they reach the emitted transcript -- same filter and drop logging as
    # the review pipeline, so every transcript surface is consistent (FW-09b).
    duration: float | None
    try:
        duration = get_video_duration(video)
    except RuntimeError:
        duration = None
    result = filter_hallucinated_segments(
        result,
        duration if duration and duration > 0 else None,
        verbose=config.verbose,
    )

    # Output
    if output:
        # Create the target directory tree so `-o some/new/dir/transcript.txt`
        # succeeds instead of crashing with a raw FileNotFoundError traceback.
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            console.print(
                f"[red]Error:[/] Cannot create output directory "
                f"[link=file://{output.parent}]{output.parent}[/link]: {e}"
            )
            raise typer.Exit(1) from None
        with open(output, "w", encoding="utf-8") as f:
            f.write(result.text)
        console.print(f"[green]Transcript saved:[/] [link=file://{output}]{output}[/link]")
    else:
        console.print()
        console.print(result.text)


@app.command()
def preprocess(
    video: Annotated[
        Path,
        typer.Argument(
            help="Path to video file",
            exists=True,
            dir_okay=False,
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output directory for preprocess artifacts",
        ),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--lang",
            "-l",
            help="Language code for transcription (defaults to config)",
        ),
    ] = None,
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Use local STT server",
        ),
    ] = False,
    include_audio: Annotated[
        bool,
        typer.Option(
            "--audio/--no-audio",
            help="Include extracted audio in the output bundle",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Reuse output directory even if preprocess artifacts already exist",
        ),
    ] = False,
) -> None:
    """
    Build a transcript-first artifact bundle for downstream review.

    This is the non-AI handoff lane: extract audio, transcribe it, save
    stable transcript artifacts, and stop before semantic or vision analysis.

    Examples:
        uv run screenscribe preprocess video.mov
        uv run screenscribe preprocess video.mov -o ./video_preprocess
        uv run screenscribe preprocess video.mov --no-audio --lang en
    """
    # Check FFmpeg is installed (shared guard — identical message across commands)
    _check_ffmpeg_or_exit()
    config = ScreenScribeConfig.load()
    if language is not None:
        config.language = language
    language = config.language

    base_output = output or (video.parent / f"{video.stem}_preprocess")
    if force:
        output_dir = base_output
    else:
        output_dir, version = _find_next_versioned_path(
            base_output,
            artifact_markers=("preprocess.json", "transcript.txt"),
        )
        if version:
            console.print(
                Panel(
                    f"[yellow]Found previous preprocess bundle at:[/] {base_output.name}\n"
                    f"[green]Creating new version:[/] {output_dir.name}",
                    title="[bold]Found Previous Preprocess Bundle[/]",
                    border_style="yellow",
                )
            )

    output_dir.mkdir(parents=True, exist_ok=True)

    console.print()
    console.print(
        Panel(
            f"[bold cyan]Screenscribe Preprocess[/]\n"
            f"[dim]Transcript-first artifact bundle for downstream model/agent review[/]\n\n"
            f"Video: [link=file://{video.resolve()}]{video.name}[/link]\n"
            f"Output: [link=file://{output_dir.resolve()}]{output_dir.resolve()}[/link]",
            border_style="cyan",
        )
    )

    audio_path = _extract_audio_or_exit(video)
    duration: float | None
    try:
        duration = get_video_duration(video)
    except RuntimeError:
        duration = None
        console.print("[yellow]Could not determine video duration[/]")

    transcription = _transcribe_audio_or_exit(
        audio_path,
        language=language,
        use_local=local,
        api_key=config.get_stt_api_key(),
        stt_endpoint=config.stt_endpoint,
        stt_model=config.stt_model,
        resume_hint=True,
    )

    # Drop no-speech hallucinations before the transcript-first bundle is written
    # -- this is the downstream-agent handoff lane, so phantom captions must not
    # ship in preprocess artifacts (same filter as the review pipeline, FW-09b).
    transcription = filter_hallucinated_segments(
        transcription,
        duration if duration and duration > 0 else None,
        verbose=config.verbose,
    )

    write_preprocess_bundle(
        video_path=video.resolve(),
        output_dir=output_dir.resolve(),
        transcription=transcription,
        duration_seconds=duration,
        extracted_audio_path=audio_path,
        include_audio=include_audio,
    )


@app.command()
def config(
    ctx: typer.Context,
    show: Annotated[
        bool,
        typer.Option(
            "--show",
            help="Show current configuration",
        ),
    ] = False,
    init: Annotated[
        bool,
        typer.Option(
            "--init",
            help="Create default config file",
        ),
    ] = False,
    set_key: Annotated[
        str | None,
        typer.Option(
            "--set-key",
            help="Set API key in config",
        ),
    ] = None,
) -> None:
    """
    Manage screenscribe configuration.

    Config file: ~/.config/screenscribe/config.env

    Examples:
        uv run screenscribe config --show
        uv run screenscribe config --init
        uv run screenscribe config --set-key YOUR_API_KEY
    """
    cfg = ScreenScribeConfig.load()

    if set_key:
        # In-place update: rewrite only the SCREENSCRIBE_API_KEY line and keep
        # every other configured value (per-endpoint keys, STT fallback,
        # api_base, user comments). A config.env.bak snapshot is taken first.
        path = cfg.save_api_key(set_key)
        console.print(f"[green]API key saved to:[/] [link=file://{path}]{path}[/link]")
        return

    if init:
        config_path = Path.home() / ".config" / "screenscribe" / "config.env"
        if config_path.exists():
            console.print(
                f"[yellow]Config already exists:[/] [link=file://{config_path}]{config_path}[/link]"
            )
            console.print("[dim]Use --show to view current config[/]")
            if not typer.confirm("Overwrite existing config?", default=False):
                console.print("[dim]Aborted. Existing config preserved.[/]")
                return
        path = cfg.save_default_config()
        console.print(f"[green]Config created:[/] [link=file://{path}]{path}[/link]")
        console.print("[dim]Edit this file to customize settings[/]")
        return

    if show:
        # Find which config file is being used
        from .config import CONFIG_PATHS, _mask_api_key

        config_source = None
        for cp in CONFIG_PATHS:
            if cp.exists():
                config_source = cp
                break

        if config_source:
            console.print(
                f"[bold]Current Configuration[/] [dim](from [link=file://{config_source}]{config_source}[/link]):[/]\n"
            )
        else:
            console.print(
                "[bold]Current Configuration[/] [dim](defaults, no config file found):[/]\n"
            )

        # API Keys
        console.print("[cyan]API Keys:[/]")
        console.print(f"  Main: {_mask_api_key(cfg.api_key)}")
        if cfg.stt_api_key and cfg.stt_api_key != cfg.api_key:
            console.print(f"  STT:  {_mask_api_key(cfg.stt_api_key)}")
        if cfg.llm_api_key and cfg.llm_api_key != cfg.api_key:
            console.print(f"  LLM:  {_mask_api_key(cfg.llm_api_key)}")
        if cfg.vision_api_key and cfg.vision_api_key != cfg.api_key:
            console.print(f"  Vision: {_mask_api_key(cfg.vision_api_key)}")
        if cfg.stt_fallback_api_key and cfg.stt_fallback_api_key != cfg.api_key:
            console.print(f"  STT-fallback: {_mask_api_key(cfg.stt_fallback_api_key)}")

        # Endpoints
        console.print("\n[cyan]Endpoints:[/]")
        console.print(f"  STT:    {cfg.stt_endpoint}")
        console.print(f"  LLM:    {cfg.llm_endpoint}")
        console.print(f"  Vision: {cfg.vision_endpoint}")
        console.print(f"  [dim](Base fallback: {cfg.api_base})[/]")

        # Models
        console.print("\n[cyan]Models:[/]")
        console.print(f"  STT:    {cfg.stt_model}")
        console.print(f"  LLM:    {cfg.llm_model}")
        console.print(f"  Vision: {cfg.vision_model}")

        # Processing
        console.print("\n[cyan]Processing:[/]")
        console.print(f"  Language: {cfg.language}")
        console.print(f"  Vision analysis: {cfg.use_vision_analysis}")

        # Keywords (passed to the AI as hints during detection)
        from .keywords import GLOBAL_KEYWORDS_PATH, KeywordsConfig

        keywords_cfg = KeywordsConfig.load()
        keywords_source = (
            f"global ({GLOBAL_KEYWORDS_PATH})"
            if GLOBAL_KEYWORDS_PATH.exists()
            else "built-in default — run `screenscribe keywords init` to customize"
        )
        console.print("\n[cyan]Keywords:[/]")
        console.print(f"  Source: {keywords_source}")
        console.print(f"  {keywords_cfg.summary()}")
        console.print(
            "  [dim]Passed to the AI as hints during detection; they do not replace "
            "LLM analysis. Manage with: screenscribe keywords[/]"
        )
        return

    # Default (no flag): show the real command help instead of an ad-hoc one-liner.
    console.print(ctx.get_help())
    raise typer.Exit()


# ---------------------------------------------------------------------------
# keywords command group
# ---------------------------------------------------------------------------
#
# Keywords are user-editable *hints* for AI detection: always on, safe when
# empty, never replacing the LLM. This command group makes the single global
# dictionary (``~/.config/screenscribe/keywords.yaml``) visible and editable.
# There is no keyword-only mode; detection is always the LLM.

keywords_app = typer.Typer(
    name="keywords",
    help=(
        "Manage keywords passed to the AI as hints during detection. "
        "Keywords are extra context (always on, safe when empty) and never "
        "replace LLM analysis."
    ),
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(keywords_app, name="keywords")

_KEYWORDS_HINT_LINE = (
    "Keywords are passed to AI as hints during detection. They do not replace LLM analysis."
)


@keywords_app.command("init")
def keywords_init() -> None:
    """Create the global keywords file from the built-in defaults.

    Writes ``~/.config/screenscribe/keywords.yaml``. An existing file is never
    overwritten without confirmation.

    Examples:
        uv run screenscribe keywords init
    """
    if GLOBAL_KEYWORDS_PATH.exists():
        console.print(
            f"[yellow]Keywords file already exists:[/] "
            f"[link=file://{GLOBAL_KEYWORDS_PATH}]{GLOBAL_KEYWORDS_PATH}[/link]"
        )
        if not typer.confirm("Overwrite with the built-in defaults?", default=False):
            console.print("[dim]Aborted. Existing keywords preserved.[/]")
            return

    save_default_keywords(GLOBAL_KEYWORDS_PATH)
    console.print("[dim]Edit this file to customize your detection keywords[/]")


@keywords_app.command("edit")
def keywords_edit() -> None:
    """Open the global keywords file in your editor.

    Uses ``$EDITOR`` (falling back to ``open`` on macOS, else ``nano``). The
    file is created from the built-in defaults first if it does not yet exist.

    Examples:
        uv run screenscribe keywords edit
    """
    if not GLOBAL_KEYWORDS_PATH.exists():
        console.print("[dim]No keywords file yet; creating from defaults...[/]")
        save_default_keywords(GLOBAL_KEYWORDS_PATH)

    _open_in_editor(GLOBAL_KEYWORDS_PATH, "keywords")


@keywords_app.command("add")
def keywords_add(
    category: Annotated[
        str,
        typer.Argument(
            help=f"Keyword category. One of: {', '.join(CATEGORIES)}.",
        ),
    ],
    phrase: Annotated[
        str,
        typer.Argument(
            help="The phrase to add (quote multi-word phrases).",
        ),
    ],
) -> None:
    """Append a phrase to a category in the global keywords file.

    Creates the file from defaults if it is missing. Only one of the six
    supported categories may be used; an unsupported category is rejected.
    An identical phrase is never duplicated.

    Examples:
        uv run screenscribe keywords add bug "klikam i nic"
        uv run screenscribe keywords add performance "za ciężkie"
    """
    if category not in CATEGORIES:
        console.print(f"[red]Error:[/] Unsupported category: {category}")
        console.print(f"[dim]Supported categories: {', '.join(CATEGORIES)}[/]")
        raise typer.Exit(1)

    phrase = phrase.strip()
    if not phrase:
        console.print("[red]Error:[/] Phrase is empty.")
        raise typer.Exit(1)

    # Create the file from defaults if it is missing, so the user has a
    # populated, well-formed dictionary to extend.
    if not GLOBAL_KEYWORDS_PATH.exists():
        save_default_keywords(GLOBAL_KEYWORDS_PATH)

    # Load the current on-disk dictionary directly (not the load() priority
    # chain) so we edit exactly the global file.
    import yaml

    try:
        with open(GLOBAL_KEYWORDS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        console.print(f"[red]Error:[/] Could not read keywords file: {e}")
        raise typer.Exit(1) from None

    if not isinstance(data, dict):
        data = {}

    existing = data.get(category)
    phrases = [str(item) for item in existing] if isinstance(existing, list) else []

    if phrase in phrases:
        console.print(
            f'[yellow]Already present:[/] {category} already contains "{phrase}" — nothing to do.'
        )
        return

    phrases.append(phrase)
    data[category] = phrases

    try:
        with open(GLOBAL_KEYWORDS_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except OSError as e:
        console.print(f"[red]Error:[/] Could not write keywords file: {e}")
        raise typer.Exit(1) from None

    console.print(f'[green]Added[/] "{phrase}" to [cyan]{category}[/].')


@keywords_app.command("list")
def keywords_list() -> None:
    """Show the active keyword dictionary.

    Reports the source (default / global / custom), the categories with their
    phrase counts, a few sample phrases, and a reminder that keywords are AI
    hints — not a replacement for LLM analysis.

    Examples:
        uv run screenscribe keywords list
    """
    config = KeywordsConfig.load()

    if GLOBAL_KEYWORDS_PATH.exists():
        console.print(
            f"[bold]Active keywords[/] [dim](source: your global dictionary — "
            f"[link=file://{GLOBAL_KEYWORDS_PATH}]{GLOBAL_KEYWORDS_PATH}[/link])[/]\n"
        )
    else:
        # Never expose the packaged read-only default path to the user — point
        # them at where their own editable dictionary lives (next to config.env).
        console.print(
            "[bold]Active keywords[/] [dim](source: built-in default — "
            "no personal dictionary yet)[/]\n"
            f"[dim]Run [bold]screenscribe keywords init[/] to create your own at "
            f"{GLOBAL_KEYWORDS_PATH}[/]\n"
        )

    for category in CATEGORIES:
        phrases = config.get_keywords(category)
        sample = ", ".join(phrases[:3])
        suffix = " ..." if len(phrases) > 3 else ""
        sample_text = f": {sample}{suffix}" if phrases else ""
        console.print(f"  [cyan]{category}[/]: {len(phrases)}{sample_text}")

    console.print(f"\n[bold]Total:[/] {config.total_keywords}")
    console.print(f"\n[dim]{_KEYWORDS_HINT_LINE}[/]")


@app.command()
def version() -> None:
    """Show version information."""
    console.print(f"[bold]Screenscribe {_version_string()}[/]")
    console.print("[dim]Turn screen recordings into engineering reports[/]")
    console.print("[dim]⌜screenscribe⌟ — Built by Vetcoders[/]")


if __name__ == "__main__":
    app()
