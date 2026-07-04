"""Image utility functions for vision analysis."""

import base64
from pathlib import Path


def encode_image_base64(image_path: Path) -> str:
    """Encode image to base64 for API."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_media_type(image_path: Path) -> str:
    """Get MIME type for image file."""
    suffix = image_path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")
