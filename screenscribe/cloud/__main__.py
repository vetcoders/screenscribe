"""``python -m screenscribe.cloud serve|worker|init-db``.

``serve`` runs the web app under uvicorn (bind defaults to loopback; the
container widens it via ``SCREENSCRIBE_CLOUD_HOST``). Proxy headers are
honoured only from peers listed in uvicorn's ``FORWARDED_ALLOW_IPS``
environment variable (default: loopback). ``worker`` runs the durable job
worker. ``init-db`` creates/migrates the SQLite schema and storage dirs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from .config import CloudSettings


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_serve(settings: CloudSettings, *, host: str, port: int, log_level: str) -> int:
    import uvicorn

    from .app import create_cloud_app

    app = create_cloud_app(settings)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        proxy_headers=True,
        server_header=False,
        timeout_graceful_shutdown=25,
    )
    return 0


def run_worker(settings: CloudSettings) -> int:
    from .worker import serve_worker

    asyncio.run(serve_worker(settings))
    return 0


def run_init_db(settings: CloudSettings) -> int:
    from .app import ensure_storage
    from .db import CloudStore
    from .security import utc_now

    ensure_storage(settings)
    store = CloudStore(settings.database_file)
    try:
        store.init_schema(utc_now())
    finally:
        store.close()
    print(f"Screenscribe Cloud schema ready at {settings.database_file.name}")
    return 0


def build_parser(settings: CloudSettings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m screenscribe.cloud", description="Screenscribe Cloud")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the web API, portal and hosted reviews")
    serve.add_argument("--host", default=settings.host, help="Bind address (env SCREENSCRIBE_CLOUD_HOST)")
    serve.add_argument("--port", type=int, default=settings.port, help="Bind port (env SCREENSCRIBE_CLOUD_PORT)")
    commands.add_parser("worker", help="Run the job worker")
    commands.add_parser("init-db", help="Create or migrate the database schema")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = CloudSettings.from_env()
    args = build_parser(settings).parse_args(argv)
    _configure_logging(args.log_level)
    if args.command == "serve":
        return run_serve(settings, host=args.host, port=args.port, log_level=args.log_level)
    if args.command == "worker":
        return run_worker(settings)
    return run_init_db(settings)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["build_parser", "main", "run_init_db", "run_serve", "run_worker"]
