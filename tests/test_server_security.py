"""Localhost security guard for the analyze server (screenscribe.server_security).

The conftest test-client auto-presents a localhost Host and the app's session
token for /api/ calls; these tests override those explicitly to exercise the
rejection paths. The guard logic is shared with the review server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.server_security import video_access_token


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        **{"api" + "_key": "test-key"},
        **{"vision_api" + "_key": "test-key"},
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def _app(sample_video: Path):
    app = create_analyze_app(sample_video, _config())
    return app, app.state.session_token


def test_index_does_not_require_token(sample_video: Path) -> None:
    app, _ = _app(sample_video)
    # "/" carries no secret and is loaded before the UI has the token.
    assert TestClient(app).get("/").status_code == 200


def test_api_rejects_missing_token(sample_video: Path) -> None:
    app, _ = _app(sample_video)
    resp = TestClient(app).get("/api/markers", headers={"X-ScreenScribe-Token": ""})
    assert resp.status_code == 403


def test_api_rejects_wrong_token(sample_video: Path) -> None:
    app, _ = _app(sample_video)
    resp = TestClient(app).get("/api/markers", headers={"X-ScreenScribe-Token": "not-the-token"})
    assert resp.status_code == 403


def test_api_accepts_correct_token(sample_video: Path) -> None:
    app, token = _app(sample_video)
    resp = TestClient(app).get("/api/markers", headers={"X-ScreenScribe-Token": token})
    assert resp.status_code == 200


def test_api_rejects_foreign_origin(sample_video: Path) -> None:
    app, token = _app(sample_video)
    resp = TestClient(app).get(
        "/api/markers",
        headers={"X-ScreenScribe-Token": token, "Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_api_accepts_localhost_origin(sample_video: Path) -> None:
    app, token = _app(sample_video)
    resp = TestClient(app).get(
        "/api/markers",
        headers={"X-ScreenScribe-Token": token, "Origin": "http://localhost:8766"},
    )
    assert resp.status_code == 200


def test_api_rejects_foreign_host(sample_video: Path) -> None:
    app, token = _app(sample_video)
    resp = TestClient(app).get(
        "/api/markers",
        headers={"X-ScreenScribe-Token": token, "Host": "evil.example"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "LOCALHOST:8766",  # case-insensitive per RFC 4343
        "localhost.:8766",  # trailing FQDN dot is the same host
        "[::1]:8766",  # bracketed IPv6 loopback
        "127.0.0.1:8766",
    ],
)
def test_host_guard_accepts_local_host_spellings(sample_video: Path, host: str) -> None:
    """Equivalent spellings of localhost must not be false-negatives — the
    guard normalizes case and the trailing dot before matching."""
    app, token = _app(sample_video)
    resp = TestClient(app).get(
        "/api/markers",
        headers={"X-ScreenScribe-Token": token, "Host": host},
    )
    assert resp.status_code == 200


def test_origin_guard_normalizes_case_and_trailing_dot(sample_video: Path) -> None:
    app, token = _app(sample_video)
    resp = TestClient(app).get(
        "/api/markers",
        headers={"X-ScreenScribe-Token": token, "Origin": "http://LOCALHOST.:8766"},
    )
    assert resp.status_code == 200


def test_bare_api_path_is_guarded(sample_video: Path) -> None:
    """/api without a trailing slash falls under the same token guard as
    /api/* — the path helper must not let it through unauthenticated."""
    app, _ = _app(sample_video)
    resp = TestClient(app).get("/api", headers={"X-ScreenScribe-Token": ""})
    # 403 from the guard, never a 200/404 that bypassed it.
    assert resp.status_code == 403


# --- Signed query token for marker frame <img> requests ---------------------
# Thumbnails load via <img src>, which cannot carry the session-token header.
# The server signs each frame_url with a per-marker HMAC ("?st=..."); the guard
# accepts that signature for GET on exactly that one path and nothing else.

# A 1x1 transparent PNG - smallest valid frame we can mark.
_PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _mark_frame(client: TestClient, token: str) -> dict:
    resp = client.post(
        "/api/mark",
        headers={"X-ScreenScribe-Token": token},
        json={"timestamp": 1.0, "frame_base64": _PNG_1X1_BASE64, "transcript": "t", "notes": ""},
    )
    assert resp.status_code == 200
    return resp.json()


def test_frame_get_accepts_signed_query_token_without_header(sample_video: Path) -> None:
    """The exact request an <img src> makes: signed URL, no token header."""
    app, token = _app(sample_video)
    client = TestClient(app)
    frame_url = _mark_frame(client, token)["frame_url"]
    assert "?st=" in frame_url
    # The empty header suppresses the conftest auto-token - like a browser <img>.
    resp = client.get(frame_url, headers={"X-ScreenScribe-Token": ""})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")


def test_frame_get_rejects_missing_signature_without_header(sample_video: Path) -> None:
    app, token = _app(sample_video)
    client = TestClient(app)
    marker_id = _mark_frame(client, token)["marker_id"]
    resp = client.get(f"/api/marker/{marker_id}/frame", headers={"X-ScreenScribe-Token": ""})
    assert resp.status_code == 403


def test_frame_get_rejects_forged_signature(sample_video: Path) -> None:
    app, token = _app(sample_video)
    client = TestClient(app)
    marker_id = _mark_frame(client, token)["marker_id"]
    resp = client.get(
        f"/api/marker/{marker_id}/frame?st={'0' * 64}",
        headers={"X-ScreenScribe-Token": ""},
    )
    assert resp.status_code == 403


def test_frame_signature_is_marker_scoped(sample_video: Path) -> None:
    """Marker A's signature must not unlock marker B's frame."""
    app, token = _app(sample_video)
    client = TestClient(app)
    signature_a = _mark_frame(client, token)["frame_url"].split("?st=", 1)[1]
    marker_b = _mark_frame(client, token)["marker_id"]
    resp = client.get(
        f"/api/marker/{marker_b}/frame?st={signature_a}",
        headers={"X-ScreenScribe-Token": ""},
    )
    assert resp.status_code == 403


def test_signed_query_token_does_not_unlock_other_api_paths(sample_video: Path) -> None:
    """The signature is scoped to GET frame - it must not weaken the guard for
    any other /api/* path or method."""
    app, token = _app(sample_video)
    client = TestClient(app)
    marked = _mark_frame(client, token)
    signature = marked["frame_url"].split("?st=", 1)[1]
    # Other API path with a valid frame signature: still 403.
    resp = client.get(f"/api/markers?st={signature}", headers={"X-ScreenScribe-Token": ""})
    assert resp.status_code == 403
    # Non-GET on the frame path itself: still 403, even with a valid signature.
    resp = client.post(marked["frame_url"], headers={"X-ScreenScribe-Token": ""})
    assert resp.status_code == 403


# --- Signed query token for the source-video <video> request ----------------
# <video src> can't carry the session-token header any more than <img src> can,
# so GET /video authenticates via the signed "?st=" video signature.


def test_video_get_accepts_signed_query_token_without_header(sample_video: Path) -> None:
    """The exact request a <video src> makes: signed URL, no token header."""
    app, token = _app(sample_video)
    client = TestClient(app)
    resp = client.get(
        f"/video?st={video_access_token(token)}",
        headers={"X-ScreenScribe-Token": ""},
    )
    assert resp.status_code == 200


def test_video_get_rejects_missing_signature_without_header(sample_video: Path) -> None:
    app, _ = _app(sample_video)
    resp = TestClient(app).get("/video", headers={"X-ScreenScribe-Token": ""})
    assert resp.status_code == 403


def test_video_get_rejects_forged_signature(sample_video: Path) -> None:
    app, _ = _app(sample_video)
    resp = TestClient(app).get(
        f"/video?st={'0' * 64}",
        headers={"X-ScreenScribe-Token": ""},
    )
    assert resp.status_code == 403


def test_video_get_accepts_session_token_header(sample_video: Path) -> None:
    """A non-browser client may still authenticate with the header instead."""
    app, token = _app(sample_video)
    resp = TestClient(app).get("/video", headers={"X-ScreenScribe-Token": token})
    assert resp.status_code == 200


def test_video_signature_does_not_unlock_api_paths(sample_video: Path) -> None:
    """The video signature is scoped to the video paths — it must not weaken the
    guard for any /api/* path."""
    app, token = _app(sample_video)
    resp = TestClient(app).get(
        f"/api/markers?st={video_access_token(token)}",
        headers={"X-ScreenScribe-Token": ""},
    )
    assert resp.status_code == 403


def test_stt_rejects_oversized_upload_with_413(sample_video: Path) -> None:
    """An upload past the 25 MB cap gets 413 — declared Content-Length is
    rejected by the middleware before the body is parsed, and the handler's
    chunked read backstops clients that lie about or omit the header."""
    app, token = _app(sample_video)
    oversized = b"\x00" * (26 * 1024 * 1024)
    resp = TestClient(app).post(
        "/api/stt",
        headers={"X-ScreenScribe-Token": token},
        files={"audio": ("big.webm", oversized, "audio/webm")},
    )
    assert resp.status_code == 413
    assert "25 MB" in resp.json()["detail"]
