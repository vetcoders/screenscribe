"""Reviewer screenshots in the agent chat and frames grabbed from the source video.

Two gaps a reviewer hit on a recording without narration: a screenshot pasted
into the chat never reached the model, and the agent could only look at the
handful of frames the pipeline had stored.

Falsification: drop the ``images`` hand-off in ``prepare_turn`` and
``test_attached_screenshot_reaches_the_responses_payload`` finds no
``input_image``; drop ``capture_video_frames`` from the toolbelt and
``test_capture_video_frames_reads_the_source_video`` gets an unknown-tool error.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.agent.chat import AgentProvider, ProviderRound, _anthropic_messages_from_input
from screenscribe.agent.context import prepare_turn
from screenscribe.agent.tools import ReportToolbelt
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app

FIXTURE = Path(__file__).parent / "fixtures" / "agent_report_synthetic.json"
_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode("ascii")


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.x.ai/v1/responses",
        stt_endpoint="https://api.x.ai/v1/stt",
        vision_endpoint="https://api.x.ai/v1/responses",
        llm_model="grok-4.6",
    )


def _app(tmp_path: Path) -> Any:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42")
    report = json.loads(FIXTURE.read_text(encoding="utf-8"))
    report["processing_provenance"] = {
        "llm": {"host": "api.x.ai", "protocol": "responses", "provider": "xai"}
    }
    (output_dir / "screen_report.json").write_text(json.dumps(report), encoding="utf-8")
    return create_review_app(output_dir, report_file.name, video_path, _config())


def test_attached_screenshot_reaches_the_responses_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: list[dict[str, Any]] = []

    async def fake_round(_provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
        payloads.append(payload)
        return ProviderRound(text="Widzę okno.", response_id="resp_1", function_calls=[])

    monkeypatch.setattr("screenscribe.agent.chat.round_tripper", fake_round)
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/agent/chat/stream",
        json={
            "message": "Co jest na tym zrzucie?",
            "history": [],
            "images": [{"media_type": "image/png", "data": _PNG}],
        },
    )
    assert response.status_code == 200, response.text
    assert "Widzę okno." in response.text
    user_turn = payloads[0]["input"][-1]
    assert user_turn["role"] == "user"
    images = [block for block in user_turn["content"] if block["type"] == "input_image"]
    assert images == [{"type": "input_image", "image_url": f"data:image/png;base64,{_PNG}"}]
    assert user_turn["content"][0] == {"type": "input_text", "text": "Co jest na tym zrzucie?"}


@pytest.mark.parametrize(
    ("image", "needle"),
    [
        ({"media_type": "image/jpeg", "data": _PNG}, "not image/jpeg"),
        ({"media_type": "image/svg+xml", "data": _PNG}, "unsupported image type"),
    ],
)
def test_attached_image_with_wrong_bytes_is_refused(
    tmp_path: Path, image: dict[str, str], needle: str
) -> None:
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/agent/chat/stream",
        json={"message": "x", "history": [], "images": [image]},
    )
    assert response.status_code == 422
    assert needle in response.text


def test_attached_image_rejects_non_base64(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/agent/chat/stream",
        json={
            "message": "x",
            "history": [],
            "images": [{"media_type": "image/png", "data": "not base64 !!!"}],
        },
    )
    assert response.status_code == 422


def test_attached_screenshot_becomes_an_anthropic_image_block() -> None:
    turn = prepare_turn(
        report={"findings": []},
        message="Look",
        images=[{"media_type": "image/png", "data": _PNG}],
    )
    messages = _anthropic_messages_from_input(turn.input_items)
    blocks = messages[-1]["content"]
    assert {"type": "text", "text": "Look"} in blocks
    assert {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG},
    } in blocks


def _tiny_video(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required to build the source clip")
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=10:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    return video


def test_capture_video_frames_reads_the_source_video(tmp_path: Path) -> None:
    toolbelt = ReportToolbelt(
        {"findings": []}, output_dir=tmp_path, video_path=_tiny_video(tmp_path)
    )
    result = json.loads(toolbelt.execute("capture_video_frames", {"timestamps": [0.5, 2.0, 99]}))
    assert result["attached"] == 2
    assert [row["attached"] for row in result["frames"]] == [True, True, False]
    assert "outside the video" in result["frames"][2]["error"]
    images = toolbelt.drain_images()
    assert [image["label"] for image in images] == ["video @ 00:00", "video @ 00:02"]
    assert all(base64.b64decode(image["data"]).startswith(b"\xff\xd8\xff") for image in images)


def test_capture_video_frames_caps_a_call_and_the_turn(tmp_path: Path) -> None:
    toolbelt = ReportToolbelt(
        {"findings": []}, output_dir=tmp_path, video_path=_tiny_video(tmp_path)
    )
    first = json.loads(
        toolbelt.execute("capture_video_frames", {"timestamps": [0, 0.5, 1, 1.5, 2]})
    )
    assert first["attached"] == 4
    assert first["omitted"] == 1
    second = json.loads(toolbelt.execute("capture_video_frames", {"timestamps": [0, 0.5, 1, 1.5]}))
    third = json.loads(toolbelt.execute("capture_video_frames", {"timestamps": [2]}))
    assert second["attached"] == 4
    assert third["attached"] == 0
    assert "budget" in third["frames"][0]["error"]


def test_capture_video_frames_without_a_video_says_so(tmp_path: Path) -> None:
    toolbelt = ReportToolbelt({"findings": []}, output_dir=tmp_path)
    result = json.loads(toolbelt.execute("capture_video_frames", {"timestamps": [1]}))
    assert "unavailable" in result["error"]
