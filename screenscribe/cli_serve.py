"""Local review-server launcher for the screenscribe ``review`` command.

Extracted from ``cli.py``. ``_serve_report`` symlinks the source video next to
the report, finds a free port, wires the review FastAPI app and opens it in the
browser. ``cli.py`` re-exports ``_serve_report`` so ``screenscribe.cli`` stays
the import surface; the browser open and console print route through the cli
module object so ``monkeypatch.setattr(cli.webbrowser, "open", ...)`` and a
patched ``cli.console`` still bind.

``serve_existing_review`` starts that same server for a directory that already
holds a ``<video>_report.html`` bundle (``screenscribe serve``).
"""

import re
import socket
from pathlib import Path
from urllib.parse import quote


def _serve_report(
    output_dir: Path,
    video_path: Path,
    port: int = 8765,
    report_filename: str | None = None,
) -> None:
    """Start interactive review server and open report in browser.

    Creates a symlink to the video in output_dir so the server can serve it,
    then starts the local review app and opens the report in the default browser.

    Args:
        output_dir: Directory containing report.html
        video_path: Path to the source video file
        port: Port for the HTTP server (default: 8765)
        report_filename: Report to serve. Defaults to ``<video stem>_report.html``;
            ``screenscribe serve`` passes the one it found when the recording was
            renamed or moved after the review.
    """
    import screenscribe.cli as cli

    console = cli.console

    report_filename = report_filename or f"{video_path.stem}_report.html"
    report_file = output_dir / report_filename
    if not report_file.exists():
        console.print(f"[yellow]No {report_filename} found, skipping server.[/]")
        return

    # Create symlink to video in output dir if not already there.
    # Only that new link is removed afterwards: a recording the operator already
    # put in the directory (file or symlink) has to stay.
    video_link = output_dir / video_path.name
    created_link = False
    if not video_link.exists() and video_path.exists():
        try:
            video_link.symlink_to(video_path.resolve())
            created_link = True
            console.print(
                f"[dim]Created symlink to video: [link=file://{video_link}]{video_link.name}[/link][/]"
            )
        except OSError as e:
            console.print(f"[yellow]Could not create video symlink: {e}[/]")

    def _find_available_port(preferred_port: int, max_tries: int = 25) -> int:
        candidate = preferred_port
        for _ in range(max_tries):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if sock.connect_ex(("127.0.0.1", candidate)) != 0:
                    return candidate
            candidate += 1
        return preferred_port

    selected_port = _find_available_port(port)
    if selected_port != port:
        console.print(
            f"[yellow]Port {port} is busy, using {selected_port} for this review server.[/]"
        )

    # Start review server
    console.print()
    console.rule("[bold cyan]Starting Review Server[/]")
    console.print(f"[dim]Serving from:[/] [link=file://{output_dir}]{output_dir}[/link]")

    try:
        import uvicorn

        from .config import ScreenScribeConfig
        from .review_server import create_review_app
        from .server_security import tokenized_url

        config = ScreenScribeConfig.load()
        app_instance = create_review_app(
            output_dir=output_dir,
            report_filename=report_filename,
            video_path=video_path,
            config=config,
        )

        # /api/* is gated by a one-time session token carried in the URL fragment.
        # The report filename comes from the video stem and routinely holds spaces
        # ("Screen Recording ... .mov"). It MUST be percent-encoded before the
        # fragment is appended: an unencoded space makes the whole string an
        # invalid URL, and macOS `open location` then escapes it wholesale --
        # including the "#" -> "%23". That turns the token into part of the request
        # path (404 + token in the access log) instead of a server-invisible
        # fragment. Encode the path first, append "#token=..." after.
        url = tokenized_url(
            f"http://localhost:{selected_port}/{quote(report_filename)}",
            app_instance.state.session_token,
        )
        console.print(f"[bold green]Report URL:[/] {url}")
        console.print()
        console.print("[dim]Press Ctrl+C to stop the server and exit[/]")
        console.print()
        console.print(f"[green]Server running on port {selected_port}[/]")
        cli.webbrowser.open(url)
        uvicorn.run(app_instance, host="127.0.0.1", port=selected_port, log_level="warning")
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping server...[/]")

    finally:
        if created_link and video_link.is_symlink():
            try:
                video_link.unlink()
            except OSError:
                pass


_VIDEO_SUFFIXES = {".mov", ".mp4", ".avi", ".mkv", ".webm", ".m4v", ".wmv"}
_REPORT_HTML_SUFFIX = "_report.html"


class ReviewDirError(Exception):
    """The path is not a review directory ``_serve_report`` can open."""


def _name_key(stem: str) -> str:
    """Compare recording names across a spaces/underscores rename."""
    return re.sub(r"[\s_]+", "_", stem).casefold()


def _list_reports(output_dir: Path) -> list[Path]:
    try:
        return sorted(
            path
            for path in output_dir.glob(f"*{_REPORT_HTML_SUFFIX}")
            if path.is_file() and not path.name.startswith(".")
        )
    except OSError as exc:
        raise ReviewDirError(f"Cannot read review directory {output_dir}: {exc}") from None


def _report_stem(report: Path) -> str:
    return report.name[: -len(_REPORT_HTML_SUFFIX)]


def _find_renamed_recording(output_dir: Path, stem: str) -> list[Path]:
    """Videos whose name matches ``stem`` once spaces and underscores are unified.

    Searched next to the review directory and next to the target of a dangling
    recording link inside it: the usual layout is ``<rec>.mov`` beside
    ``<rec>_review/``, and the link points at where the recording used to be.
    """
    folders = [output_dir.parent]
    for path in output_dir.iterdir():
        if path.is_symlink() and path.stem == stem and not path.exists():
            target_parent = Path(path.readlink()).expanduser().parent
            if target_parent.is_absolute() and target_parent not in folders:
                folders.append(target_parent)
    wanted = _name_key(stem)
    found: list[Path] = []
    for folder in folders:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            if (
                path.suffix.lower() in _VIDEO_SUFFIXES
                and _name_key(path.stem) == wanted
                and path.is_file()
                and path.resolve() not in {f.resolve() for f in found}
            ):
                found.append(path)
    return sorted(found)


def resolve_existing_review(output_dir: Path, video: Path | None = None) -> tuple[Path, Path, str]:
    """Return ``(output_dir, video_path, report_filename)`` for ``_serve_report``.

    A servable directory holds ``<video_stem>_report.html``. The video is either
    already inside that directory or passed explicitly (the recording usually
    stays outside the review folder; ``review --serve`` only symlinks it while
    the server runs). A recording renamed after the review (for example spaces
    turned into underscores) still serves: ``--video`` falls back to the only
    report in the directory, and without ``--video`` the renamed file is found
    beside the review directory.
    """
    output_dir = output_dir.expanduser()
    try:
        exists = output_dir.exists()
        is_dir = output_dir.is_dir() if exists else False
    except OSError as exc:
        raise ReviewDirError(f"Cannot read review directory {output_dir}: {exc}") from None
    if not exists:
        raise ReviewDirError(f"Review directory not found: {output_dir}")
    if not is_dir:
        raise ReviewDirError(f"Not a directory: {output_dir}")

    if video is not None:
        video = video.expanduser()
        try:
            video_ok = video.is_file()
        except OSError as exc:
            raise ReviewDirError(f"Cannot read video {video}: {exc}") from None
        if not video_ok:
            raise ReviewDirError(f"Video not found: {video}")
        report = output_dir / f"{video.stem}{_REPORT_HTML_SUFFIX}"
        if report.is_file():
            return output_dir, video, report.name
        reports = _list_reports(output_dir)
        if len(reports) == 1:
            # The recording was renamed after the review; the report keeps its
            # original name and the server rewrites its <video src> anyway.
            return output_dir, video, reports[0].name
        if not reports:
            raise ReviewDirError(
                f"No review report in {output_dir}. Expected a <video>{_REPORT_HTML_SUFFIX} file."
            )
        names = ", ".join(path.name for path in reports)
        raise ReviewDirError(
            f"No {report.name} in {output_dir}, and it holds several reports ({names}). "
            "Pass the review directory of this recording."
        )

    reports = _list_reports(output_dir)
    if not reports:
        raise ReviewDirError(
            f"No review report in {output_dir}. Expected a <video>{_REPORT_HTML_SUFFIX} file."
        )

    paired: list[tuple[Path, Path]] = []
    for report in reports:
        stem = _report_stem(report)
        try:
            videos = sorted(
                path
                for path in output_dir.iterdir()
                if path.is_file() and path.stem == stem and path.suffix.lower() in _VIDEO_SUFFIXES
            )
        except OSError as exc:
            raise ReviewDirError(f"Cannot read review directory {output_dir}: {exc}") from None
        if len(videos) > 1:
            names = ", ".join(path.name for path in videos)
            raise ReviewDirError(
                f"Multiple videos match {report.name} in {output_dir}: {names}. Pass --video."
            )
        if len(videos) == 1:
            paired.append((report, videos[0]))

    if len(paired) == 1:
        return output_dir, paired[0][1], paired[0][0].name
    if paired:
        names = ", ".join(report.name for report, _video in paired)
        raise ReviewDirError(
            f"Multiple review reports in {output_dir}: {names}. Pass --video to choose one."
        )

    if len(reports) == 1:
        renamed = _find_renamed_recording(output_dir, _report_stem(reports[0]))
        if len(renamed) == 1:
            return output_dir, renamed[0], reports[0].name
    names = ", ".join(path.name for path in reports)
    raise ReviewDirError(
        f"Found {names} in {output_dir}, but not the recording it was made from "
        "(moved, renamed or deleted). Pass --video with the original recording."
    )


def serve_existing_review(output_dir: Path, video: Path | None = None, port: int = 8765) -> None:
    """Start ``_serve_report`` for an existing review directory."""
    resolved_dir, video_path, report_filename = resolve_existing_review(output_dir, video)
    _serve_report(resolved_dir, video_path, port, report_filename=report_filename)
