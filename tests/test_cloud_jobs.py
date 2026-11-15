"""Cloud jobs: streaming upload, chunked 413 body limit, worker pipeline, dual-track audio, and SSE."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from screenscribe.cloud.app import create_cloud_app
from screenscribe.cloud.db import JobRecord
from screenscribe.cloud.worker import (
    JobContext,
    JobWorker,
    PipelineFailure,
    PipelineResult,
    _extract_job_audio,
    build_pipeline_config,
    llm_processing_receipt,
    run_pipeline,
)
from screenscribe.semantic_filter import PointOfInterest, SemanticFilterResult
from screenscribe.transcribe import Segment, TranscriptionResult
from tests.cloud_test_helpers import auth_payload, make_cloud_settings


def _seed_paid_user(client: TestClient, app: Any, email: str = "jobs@example.com") -> str:
    reg = client.post(
        "/api/v1/auth/register",
        json=auth_payload(email, "jobs-worker-phrase"),
    ).json()
    user_id = reg["user"]["user_id"]
    now = app.state.cloud.now()
    app.state.cloud.store.upsert_subscription(
        user_id,
        active=True,
        plan="pro",
        source="stripe",
        status="active",
        minutes_limit=300.0,
        now=now,
        period_start="2026-06-01T00:00:00Z",
        period_end="2026-07-01T00:00:00Z",
    )
    return str(reg["access_token"])


def test_jobs_streaming_upload_413_chunked_and_422_corrupt_and_idempotency(tmp_path: Path) -> None:
    probe_ok = {"valid": True}

    def fake_probe(_path: Path) -> float:
        if not probe_ok["valid"]:
            raise RuntimeError("ffprobe unreadable container")
        return 45.0

    settings = make_cloud_settings(tmp_path / "storage", max_upload_bytes=2048)
    app = create_cloud_app(settings, duration_probe=fake_probe)

    with TestClient(app) as client:
        bearer = _seed_paid_user(client, app)
        headers = {"Authorization": f"Bearer {bearer}"}

        # 1. Oversize with Content-Length -> 413
        big_payload = b"x" * 1_200_000
        over_cl = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"video": ("huge.mp4", big_payload, "video/mp4")},
        )
        assert over_cl.status_code == 413

        # 2. Chunked streaming body WITHOUT Content-Length exceeding cap -> clean 413 JSON!
        boundary = "chunked-limit-test"

        def chunked_oversize() -> Iterator[bytes]:
            yield (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="video"; filename="stream.mp4"\r\n'
                "Content-Type: video/mp4\r\n\r\n"
            ).encode()
            for _ in range(25):
                yield b"z" * 65536
            yield f"\r\n--{boundary}--\r\n".encode()

        over_chunked = client.post(
            "/api/v1/jobs",
            headers={
                **headers,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            content=chunked_oversize(),
        )
        assert over_chunked.status_code == 413

        # 3. Corrupt media (ffprobe failure) -> 422 without spending quota
        probe_ok["valid"] = False
        corrupt = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"file": ("broken.mp4", b"not-a-video", "video/mp4")},
        )
        assert corrupt.status_code == 422
        me_after_corrupt = client.get("/api/v1/auth/me", headers=headers).json()
        assert me_after_corrupt["subscription"]["minutes_used"] == 0.0

        # 4. Valid upload with Idempotency-Key deduplicates across retries
        probe_ok["valid"] = True
        idem_headers = {**headers, "Idempotency-Key": "idem-job-001"}
        j1 = client.post(
            "/api/v1/jobs",
            headers=idem_headers,
            data={"language": "pl", "preset": "programming", "visibility": "unlisted"},
            files={"video": ("clip.mp4", b"valid-mp4-bytes", "video/mp4")},
        )
        assert j1.status_code == 202
        j2 = client.post(
            "/api/v1/jobs",
            headers=idem_headers,
            data={"language": "pl", "preset": "programming", "visibility": "unlisted"},
            files={"file": ("clip.mp4", b"valid-mp4-bytes", "video/mp4")},
        )
        assert j2.status_code == 202
        assert j1.json()["job_id"] == j2.json()["job_id"]

        listed = client.get("/api/v1/jobs", headers=headers).json()["jobs"]
        assert len(listed) == 1


def test_worker_execution_sse_events_and_pre_ai_failure_refund(tmp_path: Path) -> None:
    now_dt = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    settings = make_cloud_settings(tmp_path / "storage", sse_poll_seconds=0.05)

    should_fail = {"flag": False}

    def stub_runner(ctx: JobContext) -> PipelineResult:
        ctx.progress("audio")
        if should_fail["flag"]:
            raise PipelineFailure("transcribe", "STT upstream down.", refundable=True)
        ctx.progress("transcribe")
        ctx.progress("report")
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        report_json = ctx.output_dir / "clip_report.json"
        report_html = ctx.output_dir / "clip_report.html"
        report_md = ctx.output_dir / "clip_report.md"
        report_json.write_text(
            json.dumps(
                {
                    "video": "clip.mp4",
                    "generated_at": "2026-06-10T12:00:00Z",
                    "executive_summary": "All clean.",
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        report_html.write_text(
            '<!doctype html><html><head><title>Clip</title></head><body><video src="clip.mp4"></video></body></html>',
            encoding="utf-8",
        )
        report_md.write_text("# Report\n", encoding="utf-8")
        return PipelineResult(
            report_json=report_json,
            report_html=report_html,
            report_md=report_md,
            language=ctx.language,
        )

    app = create_cloud_app(
        settings,
        duration_probe=lambda _p: 120.0,
        pipeline_runner=stub_runner,
        clock=lambda: now_dt,
    )

    with TestClient(app) as client:
        bearer = _seed_paid_user(client, app, "worker@example.com")
        headers = {"Authorization": f"Bearer {bearer}"}

        created = client.post(
            "/api/v1/jobs",
            headers=headers,
            data={"visibility": "unlisted", "language": "en"},
            files={"video": ("clip.mp4", b"video-stream-bytes", "video/mp4")},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        worker = JobWorker(
            settings, app.state.cloud.store, runner=stub_runner, clock=lambda: now_dt
        )
        claimed = app.state.cloud.store.claim_next_job("test-worker", now_dt)
        assert claimed is not None
        worker.process_job(claimed)

        status = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        assert status["status"] == "completed"
        assert status["share_id"] is not None
        assert status["video_stream_url"].endswith(f"/r/{status['share_id']}/video")

        # SSE stream on completed job yields job_completed immediately and closes
        with client.stream("GET", f"/api/v1/jobs/{job_id}/events", headers=headers) as sse:
            assert sse.status_code == 200
            sse_text = "".join(sse.iter_text())
        assert "event: job_completed" in sse_text
        assert status["share_id"] in sse_text

        # Pre-AI failure refunds reserved minutes automatically
        should_fail["flag"] = True
        fail_job = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"video": ("fail.mp4", b"video-stream-bytes", "video/mp4")},
        ).json()
        claimed_fail = app.state.cloud.store.claim_next_job("test-worker", now_dt)
        assert claimed_fail is not None
        worker.process_job(claimed_fail)

        with client.stream(
            "GET", f"/api/v1/jobs/{fail_job['job_id']}/events", headers=headers
        ) as sse_fail:
            fail_sse_text = "".join(sse_fail.iter_text())
        assert "event: job_failed" in fail_sse_text

        # Only the first job's 2.0 minutes remain debited; failed job's 2.0 minutes were refunded
        me = client.get("/api/v1/auth/me", headers=headers).json()
        assert me["subscription"]["minutes_used"] == 2.0


def test_extract_job_audio_dual_track_amix_and_run_pipeline_stages(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[list[str]] = []

    def fake_media_cmd(
        cmd: list[str], *, timeout: int, tool: str
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if tool == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n1\n", stderr="")
        Path(cmd[-1]).write_bytes(b"ID3-mixed-mp3")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("screenscribe.audio._run_media_command", fake_media_cmd)
    vid = tmp_path / "dual_track.mp4"
    vid.write_bytes(b"mp4-container")
    out_mp3 = tmp_path / "dual_track.audio.mp3"

    extracted = _extract_job_audio(vid, out_mp3)
    assert extracted == out_mp3
    assert any("amix=inputs=2" in arg for cmd in calls for arg in cmd)

    # Also exercise run_pipeline end-to-end with stubbed STT / LLM / screenshot stages
    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_chunked",
        lambda *_a, **_kw: TranscriptionResult(
            text="Button overlaps the footer on mobile.",
            segments=[
                Segment(id=1, start=1.0, end=4.0, text="Button overlaps the footer on mobile.")
            ],
            language="en",
            response_id="resp_stt_1",
        ),
    )
    monkeypatch.setattr(
        "screenscribe.semantic_filter.semantic_prefilter",
        lambda *_a, **_kw: SemanticFilterResult(
            pois=[
                PointOfInterest(
                    timestamp_start=1.0,
                    timestamp_end=4.0,
                    category="ui",
                    confidence=0.92,
                    reasoning="User points out footer overlap on mobile.",
                    transcript_excerpt="Button overlaps the footer on mobile.",
                    segment_ids=[1],
                )
            ]
        ),
    )

    def fake_shots(
        _video: Path, detections: list[Any], shots_dir: Path
    ) -> list[tuple[Any, Path]]:
        shots_dir.mkdir(parents=True, exist_ok=True)
        shot = shots_dir / "001.jpg"
        shot.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9")
        return [(detections[0], shot)]

    monkeypatch.setattr("screenscribe.screenshots.extract_screenshots_for_detections", fake_shots)
    monkeypatch.setattr("screenscribe.unified_analysis.analyze_all_findings_unified", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "screenscribe.summary_fallback.generate_detection_executive_summary",
        lambda *_a, **_kw: "One UI overlap detected.",
    )

    settings = make_cloud_settings(tmp_path / "storage")
    cfg = build_pipeline_config(settings, language="en", prompt="Focus on layout.")
    assert cfg.analysis_prompt_override == "Focus on layout."
    assert llm_processing_receipt(cfg) is not None

    job = JobRecord(
        job_id="job_pipe0001",
        user_id="usr_1",
        status="processing",
        stage="queued",
        progress_pct=0,
        video_filename="dual_track.mp4",
        video_path=str(vid),
        duration_seconds=12.0,
        reserved_minutes=0.25,
        language="en",
        preset="programming",
        prompt_override="Focus on layout.",
        visibility="private",
        share_id=None,
        review_id=None,
        error_message=None,
        worker_id="w1",
        idempotency_key=None,
        created_at="2026-06-10T12:00:00Z",
        updated_at="2026-06-10T12:00:00Z",
    )
    stages: list[str] = []
    res = run_pipeline(
        JobContext(
            job=job,
            video_path=vid,
            output_dir=tmp_path / "review_out",
            language="en",
            settings=settings,
            progress=stages.append,
        )
    )
    assert res.report_json.is_file()
    assert res.report_html is not None and res.report_html.is_file()
    assert "report" in stages
