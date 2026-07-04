"""Local review-server launcher for the screenscribe ``review`` command.

Extracted from ``cli.py``. ``_serve_report`` symlinks the source video next to
the report, finds a free port, wires the review FastAPI app and opens it in the
browser. ``cli.py`` re-exports ``_serve_report`` so ``screenscribe.cli`` stays
the import surface; the browser open and console print route through the cli
module object so ``monkeypatch.setattr(cli.webbrowser, "open", ...)`` and a
patched ``cli.console`` still bind.
"""

import socket
from pathlib import Path


def _serve_report(output_dir: Path, video_path: Path, port: int = 8765) -> None:
    """Start interactive review server and open report in browser.

    Creates a symlink to the video in output_dir so the server can serve it,
    then starts the local review app and opens the report in the default browser.

    Args:
        output_dir: Directory containing report.html
        video_path: Path to the source video file
        port: Port for the HTTP server (default: 8765)
    """
    import screenscribe.cli as cli

    console = cli.console

    report_filename = f"{video_path.stem}_report.html"
    report_file = output_dir / report_filename
    if not report_file.exists():
        console.print(f"[yellow]No {report_filename} found, skipping server.[/]")
        return

    # Create symlink to video in output dir if not already there
    video_link = output_dir / video_path.name
    if not video_link.exists() and video_path.exists():
        try:
            video_link.symlink_to(video_path.resolve())
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
        url = tokenized_url(
            f"http://localhost:{selected_port}/{report_filename}",
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
        # Clean up symlink
        if video_link.exists() and video_link.is_symlink():
            try:
                video_link.unlink()
            except OSError:
                pass
