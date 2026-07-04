"""Shared server-side surface shell rendering."""

from .renderer import render_surface
from .surface import ANALYZE_SURFACE, REVIEW_SURFACE, HeaderCellConfig, SurfaceConfig, TabConfig

__all__ = [
    "ANALYZE_SURFACE",
    "REVIEW_SURFACE",
    "HeaderCellConfig",
    "SurfaceConfig",
    "TabConfig",
    "render_surface",
]
