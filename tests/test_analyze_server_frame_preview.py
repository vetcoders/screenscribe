"""Tests for the captured frame preview (Group C, Issue #6).

When the user clicks "Mark Frame" in the analyze dashboard, the captured
frame must:
- be persisted to disk under the per-session frames dir,
- be reachable via ``GET /api/marker/{id}/frame`` with its real MIME type,
- expose a ``frame_url`` in both the mark response and the markers listing
  so the kafelek can render the thumbnail without an extra round trip.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import MAX_FRAME_BASE64_CHARS, create_analyze_app
from screenscribe.config import ScreenScribeConfig

# A 1x1 transparent PNG, base64 encoded - smallest valid image we can mark
# without dragging in any image library or fixture file.
PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
JPEG_FRAME_BYTES = b"\xff\xd8\xff\xe0browser-canvas-jpeg\xff\xd9"
JPEG_FRAME_BASE64 = base64.b64encode(JPEG_FRAME_BYTES).decode()


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def test_mark_frame_persists_png_and_exposes_url(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/mark",
        json={
            "timestamp": 3.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "hello",
            "notes": "",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    marker_id = payload["marker_id"]
    assert payload["status"] == "pending"
    # frame_url carries the per-marker signed query token so a header-less
    # <img src> request can authenticate (see server_security).
    assert payload["frame_url"].startswith(f"/api/marker/{marker_id}/frame?st=")

    markers = client.get("/api/markers").json()
    assert len(markers) == 1
    assert markers[0]["frame_url"] == payload["frame_url"]

    # Fetch exactly like the dashboard <img> does: signed URL, no token header
    # (the empty header suppresses the conftest auto-token).
    frame = client.get(payload["frame_url"], headers={"X-ScreenScribe-Token": ""})
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/png")
    # Body must equal the bytes we marked, otherwise the dashboard would show
    # the wrong image / a stale frame.
    assert frame.content == base64.b64decode(PNG_1X1_BASE64)


def test_mark_frame_preserves_browser_jpeg_mime(sample_video: Path) -> None:
    """The real dashboard captures JPEG frames, so the API must not lie as PNG."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/mark",
        json={
            "timestamp": 4.0,
            "frame_base64": JPEG_FRAME_BASE64,
            "transcript": "browser flow",
            "notes": "",
        },
    )
    assert response.status_code == 200
    marker_id = response.json()["marker_id"]

    frame = client.get(f"/api/marker/{marker_id}/frame")
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/jpeg")
    assert f'filename="{marker_id}.jpg"' in frame.headers["content-disposition"]
    assert frame.content == JPEG_FRAME_BYTES


def test_mark_frame_drops_base64_but_preview_still_serves(sample_video: Path) -> None:
    """412: after a successful persist the up-to-20 MB base64 copy is dropped
    from the in-memory marker (the on-disk file is the source of truth), yet the
    frame preview endpoint still serves the image from that file."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/mark",
        json={
            "timestamp": 7.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "drop base64",
            "notes": "",
        },
    )
    assert response.status_code == 200
    marker_id = response.json()["marker_id"]

    # The persisted file — not the in-memory base64 — is now the source of
    # truth, so the marker stored in the session carries an empty frame_base64.
    marker = app.state.session.markers[marker_id]
    assert marker.frame_base64 == ""
    assert marker.frame_path is not None
    assert marker.frame_path.exists()

    # The preview endpoint still serves the exact bytes, straight from disk
    # (conftest injects the session auth token like the other preview tests).
    frame = client.get(f"/api/marker/{marker_id}/frame")
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/png")
    assert frame.content == base64.b64decode(PNG_1X1_BASE64)


@pytest.mark.parametrize(
    ("frame_base64", "expected_detail"),
    [
        ("", "JPEG or PNG"),
        ("not-base64", "valid base64"),
        (base64.b64encode(b"not an image").decode(), "JPEG or PNG"),
    ],
)
def test_mark_frame_rejects_invalid_frame_without_saving_marker(
    sample_video: Path, frame_base64: str, expected_detail: str
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": frame_base64,
            "transcript": "invalid frame",
            "notes": "",
        },
    )

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]
    assert client.get("/api/markers").json() == []


def test_mark_frame_rejects_oversize_frame_without_saving_marker(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": "A" * (MAX_FRAME_BASE64_CHARS + 1),
            "transcript": "too large",
            "notes": "",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid frame_base64"
    assert client.get("/api/markers").json() == []


def test_get_frame_returns_404_for_unknown_marker(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/api/marker/does-not-exist/frame")
    assert response.status_code == 404


def test_dashboard_includes_frame_modal(sample_video: Path) -> None:
    """The HTML page wires the frame preview modal so thumbnails can enlarge."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'id="frameModal"' in html
    assert "openFrameModal" in html
    assert "marker-thumb" in html
    assert 'class="frame-modal-close"' in html
    assert 'aria-label="Close"' in html
    assert "event.target === frameModal" in html
