"""Cloud publish ingestion, zip-slip archive rejection, /r/{share_id} visibility, Range 206, and sandboxed agent."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import tarfile
import zlib
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from screenscribe.agent.tools import responses_tool_schemas
from screenscribe.cloud.app import create_cloud_app
from screenscribe.cloud.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, isoformat_z
from tests.cloud_test_helpers import auth_payload, make_cloud_settings


def _register_and_subscribe(client: TestClient, app: Any, email: str) -> str:
    reg = client.post(
        "/api/v1/auth/register",
        json=auth_payload(email, "review-owner-phrase"),
    ).json()
    now = app.state.cloud.now()
    app.state.cloud.store.upsert_subscription(
        reg["user"]["user_id"],
        active=True,
        plan="pro",
        source="stripe",
        status="active",
        minutes_limit=300.0,
        now=now,
        period_start=isoformat_z(now - timedelta(days=1)),
        period_end=isoformat_z(now + timedelta(days=30)),
    )
    return str(reg["access_token"])


def _build_tar_gz(members: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink is not None:
            sym = tarfile.TarInfo(symlink[0])
            sym.type = tarfile.SYMTYPE
            sym.linkname = symlink[1]
            tf.addfile(sym)
    return buf.getvalue()


def test_publish_bundle_zip_slip_symlink_rejection_and_idempotency(tmp_path: Path) -> None:
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings)

    with TestClient(app) as client:
        owner_bearer = _register_and_subscribe(client, app, "pub@example.com")
        headers = {"Authorization": f"Bearer {owner_bearer}"}

        # 1. Zip-slip traversal '../evil.txt' rejected before extraction
        evil_tar = _build_tar_gz({"../evil.txt": b"escape"})
        manifest = {
            "schema_version": "publish_manifest.v1",
            "video_stem": "demo",
            "video_included": False,
            "visibility": "private",
        }
        res_slip = client.post(
            "/api/v1/reviews/publish",
            headers=headers,
            data={"manifest": json.dumps(manifest)},
            files={"bundle": ("bundle.tar.gz", evil_tar, "application/gzip")},
        )
        assert res_slip.status_code == 400
        assert not (tmp_path / "evil.txt").exists()

        # 2. Symlink member inside tar rejected
        sym_tar = _build_tar_gz(
            {"demo_report.json": json.dumps({"video": "demo.mp4", "findings": []}).encode()},
            symlink=("screenshots/leak.jpg", "/etc/hosts"),
        )
        res_sym = client.post(
            "/api/v1/reviews/publish",
            headers=headers,
            data={"manifest": json.dumps(manifest)},
            files={"bundle": ("bundle.tar.gz", sym_tar, "application/gzip")},
        )
        assert res_sym.status_code == 400

        # 3. Valid bundle + video streams cleanly, regenerates HTML, and replays Idempotency-Key
        video_bytes = b"".join(f"frame-{idx:04d}-payload-".encode() for idx in range(120))
        video_sha = hashlib.sha256(video_bytes).hexdigest()
        jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        report_obj = {
            "video": "demo.mp4",
            "generated_at": "2026-06-15T12:00:00Z",
            "executive_summary": "Header misaligned.",
            "transcript": "Header is misaligned by 8px.",
            "transcript_segments": [
                {"id": 1, "start": 1.5, "end": 4.0, "text": "Header is misaligned by 8px."}
            ],
            "findings": [
                {
                    "id": 1,
                    "category": "ui",
                    "timestamp_start": 1.5,
                    "timestamp_end": 4.0,
                    "timestamp_formatted": "00:01",
                    "text": "Header is misaligned by 8px.",
                    "context": "Top navbar",
                    "keywords": ["misaligned"],
                    "screenshot": "001.jpg",
                    "unified_analysis": {
                        "status": "completed",
                        "is_issue": True,
                        "sentiment": "negative",
                        "severity": "medium",
                        "summary": "Header misaligned",
                        "action_items": ["Align header padding"],
                    },
                }
            ],
        }
        review_state_obj = {
            "reviewer": "Maciej",
            "reviewed_at": "2026-06-15T12:05:00Z",
            "findings": {
                "1": {
                    "verdict": "confirmed",
                    "severity": "high",
                    "notes": "Confirmed on retina display",
                    "actionItems": "Fix CSS grid",
                }
            },
        }
        clean_tar = _build_tar_gz(
            {
                "demo_report.json": json.dumps(report_obj).encode("utf-8"),
                "review_state.json": json.dumps(review_state_obj).encode("utf-8"),
                "screenshots/001.jpg": jpeg_bytes,
            }
        )
        full_manifest = {
            "schema_version": "publish_manifest.v1",
            "video_name": "demo.mp4",
            "video_sha256": video_sha,
            "video_size_bytes": len(video_bytes),
            "duration_seconds": 12.5,
            "language": "en",
            "generated_at": "2026-06-15T12:00:00Z",
            "screenscribe_version": "0.1.21",
            "visibility": "private",
            "title": "Demo Review",
        }
        idem_headers = {**headers, "Idempotency-Key": "pub-idem-key-1"}
        pub1 = client.post(
            "/api/v1/reviews/publish",
            headers=idem_headers,
            data={"manifest": json.dumps(full_manifest)},
            files={
                "bundle": ("bundle.tar.gz", clean_tar, "application/gzip"),
                "video": ("demo.mp4", video_bytes, "video/mp4"),
            },
        )
        assert pub1.status_code == 201
        body1 = pub1.json()
        share_id = body1["share_id"]
        assert body1["share_url"] == f"http://testserver/r/{share_id}"
        assert body1["video_stream_url"] == f"http://testserver/r/{share_id}/video"
        assert (
            body1["report_json_url"] == f"http://testserver/api/v1/reviews/{share_id}/report.json"
        )

        pub2 = client.post(
            "/api/v1/reviews/publish",
            headers=idem_headers,
            data={"manifest": json.dumps(full_manifest)},
            files={
                "bundle": ("bundle.tar.gz", clean_tar, "application/gzip"),
                "video": ("demo.mp4", video_bytes, "video/mp4"),
            },
        )
        assert pub2.status_code == 201
        assert pub2.json()["share_id"] == share_id


def _make_1x1_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_hosted_review_visibility_bearer_cookie_bridge_range_206_and_sandboxed_agent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = make_cloud_settings(tmp_path / "storage")
    app = create_cloud_app(settings)

    video_bytes = bytes(range(256)) * 20  # 5120 bytes
    video_sha = hashlib.sha256(video_bytes).hexdigest()
    png_1x1 = _make_1x1_png()
    png_b64 = base64.b64encode(png_1x1).decode("ascii")

    with (
        TestClient(app) as owner_client,
        TestClient(app) as anon_client,
        TestClient(app) as other_client,
    ):
        owner_bearer = _register_and_subscribe(owner_client, app, "owner@example.com")
        other_reg = other_client.post(
            "/api/v1/auth/register",
            json=auth_payload("other@example.com", "other-user-phrase"),
        ).json()
        other_bearer = other_reg["access_token"]

        bundle_bytes = _build_tar_gz(
            {
                "vid_report.json": json.dumps(
                    {
                        "video": "vid.mp4",
                        "generated_at": "2026-06-15T12:00:00Z",
                        "findings": [
                            {
                                "id": 1,
                                "category": "bug",
                                "timestamp_start": 2.0,
                                "timestamp_end": 3.0,
                                "text": "Crash on submit",
                            }
                        ],
                    }
                ).encode("utf-8")
            }
        )
        priv_manifest = {
            "schema_version": "publish_manifest.v1",
            "video_name": "vid.mp4",
            "video_sha256": video_sha,
            "video_size_bytes": len(video_bytes),
            "visibility": "private",
        }
        priv_pub = owner_client.post(
            "/api/v1/reviews/publish",
            headers={"Authorization": f"Bearer {owner_bearer}"},
            data={"manifest": json.dumps(priv_manifest)},
            files={
                "bundle": ("bundle.tar.gz", bundle_bytes, "application/gzip"),
                "video": ("vid.mp4", video_bytes, "video/mp4"),
            },
        ).json()
        priv_share = priv_pub["share_id"]

        # Anonymous -> 401 gate page; other user -> 403 gate page
        assert anon_client.get(f"/r/{priv_share}").status_code == 401
        assert (
            other_client.get(
                f"/r/{priv_share}", headers={"Authorization": f"Bearer {other_bearer}"}
            ).status_code
            == 403
        )
        assert (
            other_client.delete(
                f"/api/v1/reviews/{priv_share}",
                headers={"Authorization": f"Bearer {other_bearer}"},
            ).status_code
            == 403
        )

        # Native macOS WKWebView opens /r/{share_id} with Authorization: Bearer -> mints session & CSRF cookies!
        page_res = owner_client.get(
            f"/r/{priv_share}", headers={"Authorization": f"Bearer {owner_bearer}"}
        )
        assert page_res.status_code == 200
        assert f'<base href="/r/{priv_share}/">' in page_res.text
        assert "window.fetch" in page_res.text
        assert f"/r/{priv_share}/video" in page_res.text
        assert SESSION_COOKIE in owner_client.cookies
        csrf_val = owner_client.cookies.get(CSRF_COOKIE) or ""
        assert csrf_val

        # Subsequent <video> Range requests work via minted cookie WITHOUT Bearer header!
        head_vid = owner_client.head(f"/r/{priv_share}/video")
        assert head_vid.status_code == 200
        assert head_vid.headers["Accept-Ranges"] == "bytes"
        assert head_vid.headers["Content-Length"] == str(len(video_bytes))

        r_first = owner_client.get(f"/r/{priv_share}/video", headers={"Range": "bytes=0-99"})
        assert r_first.status_code == 206
        assert r_first.headers["Content-Range"] == f"bytes 0-99/{len(video_bytes)}"
        assert r_first.content == video_bytes[:100]

        r_open = owner_client.get(f"/r/{priv_share}/video", headers={"Range": "bytes=5000-"})
        assert r_open.status_code == 206
        assert r_open.content == video_bytes[5000:]

        r_suffix = owner_client.get(f"/r/{priv_share}/video", headers={"Range": "bytes=-50"})
        assert r_suffix.status_code == 206
        assert r_suffix.content == video_bytes[-50:]

        r_bad = owner_client.get(f"/r/{priv_share}/video", headers={"Range": "bytes=99999-100000"})
        assert r_bad.status_code == 416

        # Review state, save, manual frame, corrections, export manifest, and reset
        csrf_headers = {"Origin": "http://testserver", CSRF_HEADER: csrf_val}
        state_0 = owner_client.get(f"/r/{priv_share}/api/review-state")
        assert state_0.status_code == 200

        save_res = owner_client.post(
            f"/r/{priv_share}/api/save",
            headers=csrf_headers,
            json={
                "reviewer": "Monika",
                "findings": {"1": {"verdict": "confirmed", "severity": "critical", "notes": "P0"}},
            },
        )
        assert save_res.status_code == 200

        mark_res = owner_client.post(
            f"/r/{priv_share}/api/manual-mark",
            headers=csrf_headers,
            json={
                "timestamp": 3.5,
                "frame_base64": png_b64,
                "transcript": "Extra frame",
                "notes": "Button glitch",
                "reset_generation": 0,
            },
        )
        assert mark_res.status_code == 200
        marker_id = mark_res.json()["marker_id"]

        patch_mark = owner_client.patch(
            f"/r/{priv_share}/api/manual-mark/{marker_id}",
            headers=csrf_headers,
            json={"notes": "Updated glitch note", "severity": "high"},
        )
        assert patch_mark.status_code == 200

        corr_res = owner_client.post(
            f"/r/{priv_share}/api/corrections",
            headers=csrf_headers,
            json={"match": "Screenscribe", "replacement": "Screenscribe Cloud", "scope": "report"},
        )
        assert corr_res.status_code == 200

        manifest_res = owner_client.get(f"/r/{priv_share}/api/export/agent-manifest")
        assert manifest_res.status_code == 200
        assert manifest_res.json()["meta"]["share_id"] == priv_share

        # Hosted Floating Agent: verify ReportToolbelt has repo_root=None (zero host-filesystem tools!)
        captured_tools: list[Any] = []

        async def fake_collect_chat(**kwargs: Any) -> dict[str, Any]:
            captured_tools.append(kwargs["tools"])
            return {"reply": "Checked finding #1.", "response_id": "resp_agent_1"}

        monkeypatch.setattr("screenscribe.cloud.review_host.collect_agent_chat", fake_collect_chat)
        chat_res = owner_client.post(
            f"/r/{priv_share}/api/agent/chat",
            headers=csrf_headers,
            json={"message": "Summarize finding 1"},
        )
        assert chat_res.status_code == 200
        assert len(captured_tools) == 1
        toolbelt = captured_tools[0]
        assert toolbelt.repo_root is None
        tool_names = {
            spec["name"]
            for spec in responses_tool_schemas(include_repo=toolbelt.repo_root is not None)
        }
        assert "open_repo_file" not in tool_names
        repo_attempt = json.loads(toolbelt.execute("open_repo_file", {"path": "pyproject.toml"}))
        assert "error" in repo_attempt

        # Reset review clears human verdicts and increments reset_generation
        reset_res = owner_client.post(f"/r/{priv_share}/api/reset-review", headers=csrf_headers)
        assert reset_res.status_code == 200

        # Publish unlisted share and verify anonymous capability access + non-owner reset refusal
        unlisted_pub = owner_client.post(
            "/api/v1/reviews/publish",
            headers={"Authorization": f"Bearer {owner_bearer}"},
            data={"manifest": json.dumps({**priv_manifest, "visibility": "unlisted"})},
            files={
                "bundle": ("bundle.tar.gz", bundle_bytes, "application/gzip"),
                "video": ("vid.mp4", video_bytes, "video/mp4"),
            },
        ).json()
        unlisted_share = unlisted_pub["share_id"]
        anon_page = anon_client.get(f"/r/{unlisted_share}")
        assert anon_page.status_code == 200
        anon_csrf = anon_client.cookies.get(CSRF_COOKIE) or ""
        assert (
            anon_client.post(
                f"/r/{unlisted_share}/api/reset-review",
                headers={"Origin": "http://testserver", CSRF_HEADER: anon_csrf},
            ).status_code
            == 403
        )

        # Owner listing and deletion
        listed = owner_client.get(
            "/api/v1/reviews", headers={"Authorization": f"Bearer {owner_bearer}"}
        ).json()["reviews"]
        assert len(listed) == 2
        del_res = owner_client.delete(
            f"/api/v1/reviews/{priv_share}",
            headers={"Authorization": f"Bearer {owner_bearer}"},
        )
        assert del_res.status_code == 200
        assert anon_client.get(f"/r/{priv_share}").status_code == 404
