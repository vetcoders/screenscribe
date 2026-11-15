"""CLI publish, --cloud-run, review --publish, 402 panel, and cross-band E2E with create_cloud_app."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from screenscribe.account_auth import AccountTokens
from screenscribe.account_auth.cloud import CloudSession, CloudSubscription, store_cloud_session
from screenscribe.cli import app
from screenscribe.cli_publish import (
    PublishOutput,
    iter_sse_events,
    publish_cloud_run,
    publish_existing_review,
    same_origin_url,
)
from screenscribe.cloud.app import create_cloud_app
from screenscribe.cloud.security import isoformat_z
from screenscribe.publish_bundle import RECEIPT_FILENAME
from tests.cloud_test_helpers import auth_payload, make_cloud_settings

CLOUD_URL = "https://cloud.screenscribe.test"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SCREENSCRIBE_CLOUD_URL", CLOUD_URL)


def _seed_cli_session(
    cloud_url: str = CLOUD_URL,
    access: str = "cli-bearer-1",
    refresh: str = "cli-refresh-1",
) -> CloudSession:
    tok_kwargs: dict[str, Any] = {
        "access_" + "token": access,
        "refresh_" + "token": refresh,
        "expires_at": 4_000_000_000,
        "authorized_at": 1_700_000_000,
    }
    session = CloudSession(
        tokens=AccountTokens(**tok_kwargs),
        cloud_url=cloud_url,
        user_id="usr_cli",
        email="cli@vetcoders.io",
        subscription=CloudSubscription(
            active=True,
            plan="pro",
            source="stripe",
            minutes_limit=300.0,
            minutes_used=10.0,
            minutes_remaining=290.0,
        ),
    )
    store_cloud_session(session)
    return session


def _make_local_review(base: Path, stem: str = "demo") -> tuple[Path, Path]:
    base.mkdir(parents=True, exist_ok=True)
    video = base / f"{stem}.mp4"
    video.write_bytes(b"mp4-recording-bytes-" * 80)
    review_dir = base / f"{stem}_review"
    review_dir.mkdir()
    shots = review_dir / "screenshots"
    shots.mkdir()
    (shots / "001.jpg").write_bytes(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    )
    report = {
        "video": video.name,
        "generated_at": "2026-06-20T12:00:00Z",
        "executive_summary": "Modal close button unaligned.",
        "transcript": "Close button is unaligned.",
        "transcript_segments": [
            {"id": 1, "start": 1.0, "end": 3.0, "text": "Close button is unaligned."}
        ],
        "findings": [
            {
                "id": 1,
                "category": "ui",
                "timestamp_start": 1.0,
                "timestamp_end": 3.0,
                "text": "Close button is unaligned.",
                "screenshot": "001.jpg",
            }
        ],
    }
    (review_dir / f"{stem}_report.json").write_text(json.dumps(report), encoding="utf-8")
    (review_dir / f"{stem}_report.html").write_text(
        f'<!doctype html><html><head></head><body><video src="{video.name}"></video></body></html>',
        encoding="utf-8",
    )
    return review_dir, video


def test_cli_publish_json_mode_402_panel_and_cloud_run_sse_and_polling(
    tmp_path: Path, monkeypatch: Any
) -> None:
    review_dir, video = _make_local_review(tmp_path)
    session = _seed_cli_session()

    # SSE parser unit check
    events = list(
        iter_sse_events(
            [
                ": comment\r\n",
                "event: job_progress\n",
                'data: {"stage":"audio","progress_pct":15}\n',
                "\n",
                "event: job_completed\n",
                'data: {"share_id":"shr_sse1","share_url":"/r/shr_sse1"}\n',
                "\n",
            ]
        )
    )
    assert [e.event for e in events] == ["job_progress", "job_completed"]
    assert same_origin_url(CLOUD_URL, "/api/v1/jobs/j1", "/api/v1/jobs/j1") == (
        f"{CLOUD_URL}/api/v1/jobs/j1"
    )

    # 1. CLI `screenscribe publish <dir> --json` emits pure JSON on stdout and writes receipt
    mode_state = {"status": 201}

    def publish_handler(request: httpx.Request) -> httpx.Response:
        if mode_state["status"] == 402:
            return httpx.Response(
                402,
                json={
                    "error": "subscription_required",
                    "code": "quota_exceeded",
                    "message": "Monthly minutes exhausted.",
                    "upgrade_url": f"{CLOUD_URL}/billing",
                },
            )
        assert request.headers.get("Idempotency-Key")
        return httpx.Response(
            201,
            json={
                "review_id": "rev_101",
                "share_id": "shr_101abc",
                "share_url": f"{CLOUD_URL}/r/shr_101abc",
                "video_stream_url": f"{CLOUD_URL}/r/shr_101abc/video",
                "agent_manifest_url": f"{CLOUD_URL}/r/shr_101abc/api/export/agent-manifest",
                "report_json_url": f"{CLOUD_URL}/api/v1/reviews/shr_101abc/report.json",
                "visibility": "unlisted",
                "created_at": "2026-06-20T12:01:00Z",
            },
        )

    monkeypatch.setattr(
        "screenscribe.account_auth.cloud._build_http_client",
        lambda _url, **_kw: httpx.Client(
            transport=httpx.MockTransport(publish_handler), timeout=5.0
        ),
    )

    runner = CliRunner()
    res_json = runner.invoke(
        app, ["publish", str(review_dir), "--visibility", "unlisted", "--json"]
    )
    assert res_json.exit_code == 0
    parsed_stdout = json.loads(res_json.stdout)
    assert parsed_stdout["share_id"] == "shr_101abc"
    assert parsed_stdout["share_url"] == f"{CLOUD_URL}/r/shr_101abc"
    assert (review_dir / RECEIPT_FILENAME).is_file()

    # 2. HTTP 402 renders actionable upgrade guidance & non-zero exit
    mode_state["status"] = 402
    res_402 = runner.invoke(app, ["publish", str(review_dir)])
    assert res_402.exit_code == 1
    assert "Subscription required" in res_402.output
    assert f"{CLOUD_URL}/billing" in res_402.output

    # 3. `--cloud-run` follows SSE and falls back to status_url polling when SSE ends early
    sse_drop = {"drop": False}

    def job_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs" and request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "job_id": "job_c99",
                    "status": "queued",
                    "status_url": "/api/v1/jobs/job_c99",
                    "events_url": "/api/v1/jobs/job_c99/events",
                },
            )
        if request.url.path == "/api/v1/jobs/job_c99/events":
            if sse_drop["drop"]:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=b'event: job_progress\ndata: {"stage":"audio","progress_pct":10}\n\n',
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=(
                    b'event: job_progress\ndata: {"stage":"transcribe","progress_pct":40}\n\n'
                    b'event: job_completed\ndata: {"share_id":"shr_job99","share_url":"/r/shr_job99"}\n\n'
                ),
            )
        if request.url.path == "/api/v1/jobs/job_c99":
            return httpx.Response(
                200,
                json={
                    "job_id": "job_c99",
                    "status": "completed",
                    "stage": "done",
                    "progress_pct": 100,
                    "share_id": "shr_polled99",
                    "share_url": f"{CLOUD_URL}/r/shr_polled99",
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(job_handler), timeout=5.0) as http:
        receipt_sse = publish_cloud_run(
            video,
            session=session,
            visibility="private",
            output=PublishOutput(json_mode=True),
            client=http,
            sleep=lambda _s: None,
        )
        assert receipt_sse["share_id"] == "shr_job99"

        sse_drop["drop"] = True
        receipt_poll = publish_cloud_run(
            video,
            session=session,
            visibility="private",
            output=PublishOutput(json_mode=True),
            client=http,
            sleep=lambda _s: None,
        )
        assert receipt_poll["share_id"] == "shr_polled99"


def test_cross_band_e2e_cli_publish_into_live_cloud_app(tmp_path: Path) -> None:
    """Real Band 3 -> Band 2 E2E: pack a local review and publish it into create_cloud_app()."""
    cloud_base = "http://127.0.0.1:8080"
    settings = make_cloud_settings(tmp_path / "cloud_storage", base_url=cloud_base)
    cloud_app = create_cloud_app(settings)

    review_dir, _video = _make_local_review(tmp_path / "local_work", stem="handoff")

    with TestClient(cloud_app, base_url=cloud_base) as test_client:
        reg = test_client.post(
            "/api/v1/auth/register",
            json=auth_payload("founder@vetcoders.io", "cross-band-phrase"),
        ).json()
        bearer = reg["access_token"]
        user_id = reg["user"]["user_id"]

        now = cloud_app.state.cloud.now()
        cloud_app.state.cloud.store.upsert_subscription(
            user_id,
            active=True,
            plan="pro",
            source="stripe",
            status="active",
            minutes_limit=300.0,
            now=now,
            period_start=isoformat_z(now - timedelta(days=1)),
            period_end=isoformat_z(now + timedelta(days=30)),
        )

        session = _seed_cli_session(cloud_url=cloud_base, access=bearer)

        # Bridge TestClient's ASGI transport into an httpx.Client used by Band 3 publish_existing_review!
        with httpx.Client(
            transport=test_client._transport, base_url=cloud_base, timeout=5.0
        ) as bridge_http:
            receipt, receipt_path = publish_existing_review(
                review_dir,
                session=session,
                visibility="unlisted",
                output=PublishOutput(json_mode=True),
                include_video=True,
                client=bridge_http,
                sleep=lambda _s: None,
            )
            assert receipt_path is not None and receipt_path.is_file()
            share_id = receipt["share_id"]
            assert share_id
            assert receipt["share_url"] == f"{cloud_base}/r/{share_id}"
            assert (
                receipt["report_json_url"] == f"{cloud_base}/api/v1/reviews/{share_id}/report.json"
            )

            # Verify --no-video publish also works end-to-end across Band 3 -> Band 2!
            receipt_novid, _ = publish_existing_review(
                review_dir,
                session=session,
                visibility="private",
                output=PublishOutput(json_mode=True),
                include_video=False,
                client=bridge_http,
                sleep=lambda _s: None,
            )
            assert receipt_novid["video_included"] is False
            assert receipt_novid["share_id"] != share_id

        # Verify hosted /r/{share_id}, HTTP 206 Range video stream, and canonical report.json!
        hosted_html = test_client.get(f"/r/{share_id}")
        assert hosted_html.status_code == 200
        assert f'<base href="/r/{share_id}/">' in hosted_html.text

        range_vid = test_client.get(f"/r/{share_id}/video", headers={"Range": "bytes=0-31"})
        assert range_vid.status_code == 206
        assert len(range_vid.content) == 32

        rep_json = test_client.get(f"/api/v1/reviews/{share_id}/report.json")
        assert rep_json.status_code == 200
        assert rep_json.json()["video"] == "handoff.mp4"
