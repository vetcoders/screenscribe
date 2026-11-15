"""Screenscribe Cloud: hosted web SaaS backend (API, portal, hosted reviews, worker).

Import cost stays low: the FastAPI app (and the pipeline it pulls in) is only
loaded when :func:`create_cloud_app` is first used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import CloudSettings

if TYPE_CHECKING:
    from .app import create_cloud_app


def __getattr__(name: str) -> Any:
    if name == "create_cloud_app":
        from .app import create_cloud_app

        return create_cloud_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["CloudSettings", "create_cloud_app"]
