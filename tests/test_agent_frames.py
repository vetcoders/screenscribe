"""Review agent sees the real frames: get_finding detail + view_frame pixels."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

import screenscribe.agent.chat as agent_chat
from screenscribe.agent.chat import AgentProvider, FunctionCall, ProviderRound
from screenscribe.agent.context import prepare_turn, seed_report_context
from screenscribe.agent.tools import READ_TOOL_NAMES, ReportToolbelt, responses_tool_schemas
from screenscribe.config import ScreenScribeConfig

JPEG = b"\xff\xd8\xff\xe0" + b"frame-pixels" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"manual-pixels" * 8


def _report() -> dict[str, Any]:
    return {
        "executive_summary": "launcher handoff",
        "findings": [
            {
                "id": 1,
                "category": "bug",
                "timestamp_start": 562.0,
                "text": "Handoff stopped on Launchpad.",
                "context": "Claude already works in screenscribe.",
                "screenshot": "01_bug_09-22.jpg",
                "unified_analysis": {
                    "summary": "Auto-recovery did not work.",
                    "severity": "high",
                    "ui_elements": ["Session rail", "Agent terminal pane"],
                    "issues_detected": ["No visible handoff error message."],
                    "technical_observations": "Agent pane shows Unfurling.",
                    "response_id": "resp_secret_chain",
                },
                "merged_frames": [
                    {
                        "id": 1000002,
                        "timestamp_start": 719.4,
                        "text": "Shell ignored the command.",
                        "screenshot": "03_bug_11-59.jpg",
                    }
                ],
            }
        ],
        "manual_review": {
            "markers": [
                {
                    "marker_id": "m-1",
                    "timestamp": 30.0,
                    "frame_path": "manual_frames/m-1.png",
                    "notes": "reviewer mark",
                    "frame_base64": "QUJD",
                }
            ]
        },
    }


@pytest.fixture
def report_dir(tmp_path: Path) -> Path:
    shots = tmp_path / "screenshots"
    shots.mkdir()
    (shots / "01_bug_09-22.jpg").write_bytes(JPEG)
    (shots / "03_bug_11-59.jpg").write_bytes(JPEG + b"member")
    manual = tmp_path / "manual_frames"
    manual.mkdir()
    (manual / "m-1.png").write_bytes(PNG)
    return tmp_path


def _belt(report_dir: Path, report: dict[str, Any] | None = None) -> ReportToolbelt:
    return ReportToolbelt(report or _report(), output_dir=report_dir)


def test_frame_tools_are_exposed_as_read_tools() -> None:
    names = {tool["name"] for tool in responses_tool_schemas(include_repo=False)}
    assert {"get_finding", "view_frame"} <= names
    assert {"get_finding", "view_frame"} <= set(READ_TOOL_NAMES)


def test_get_finding_returns_full_visual_analysis_without_pixels(report_dir: Path) -> None:
    result = json.loads(_belt(report_dir).execute("get_finding", {"finding_id": 1}))
    unified = result["unified_analysis"]
    assert unified["ui_elements"] == ["Session rail", "Agent terminal pane"]
    assert unified["issues_detected"] == ["No visible handoff error message."]
    assert "response_id" not in unified
    assert [frame["frame"] for frame in result["frames"]] == [
        "01_bug_09-22.jpg",
        "03_bug_11-59.jpg",
    ]


def test_get_finding_resolves_member_and_manual_marker(report_dir: Path) -> None:
    belt = _belt(report_dir)
    member = json.loads(belt.execute("get_finding", {"finding_id": 1000002}))
    assert member["id"] == 1 and member["matched_member_id"] == 1000002
    marker = json.loads(belt.execute("get_finding", {"finding_id": "m-1"}))
    assert marker["kind"] == "manual_marker"
    assert "frame_base64" not in marker


def test_view_frame_attaches_finding_and_merged_frames(report_dir: Path) -> None:
    belt = _belt(report_dir)
    raw = belt.execute("view_frame", {"finding_id": 1})
    result = json.loads(raw)
    assert result["attached"] == 2
    assert base64.b64encode(JPEG).decode() not in raw
    images = belt.drain_images()
    assert [base64.b64decode(img["data"]) for img in images] == [JPEG, JPEG + b"member"]
    assert all(img["media_type"] == "image/jpeg" for img in images)
    assert belt.drain_images() == []


def test_view_frame_member_timestamp_frame_and_manual(report_dir: Path) -> None:
    belt = _belt(report_dir)
    member = json.loads(belt.execute("view_frame", {"finding_id": 1000002}))
    assert [row["frame"] for row in member["frames"]] == ["03_bug_11-59.jpg"]
    nearest = json.loads(belt.execute("view_frame", {"timestamp": 700}))
    assert [row["frame"] for row in nearest["frames"]] == ["03_bug_11-59.jpg"]
    named = json.loads(belt.execute("view_frame", {"frame": "01_bug_09-22.jpg"}))
    assert named["attached"] == 1
    manual = json.loads(belt.execute("view_frame", {"finding_id": "m-1"}))
    assert manual["attached"] == 1
    images = belt.drain_images()
    assert images[-1]["media_type"] == "image/png"
    assert len(images) == 4


def test_view_frame_refuses_escape_missing_and_non_images(report_dir: Path) -> None:
    report = _report()
    report["findings"][0]["screenshot"] = "../../etc/passwd"
    report["findings"][0]["merged_frames"][0]["screenshot"] = "missing.jpg"
    (report_dir / "not_image.jpg").write_bytes(b"plain text")
    report["findings"].append({"id": 2, "timestamp_start": 1.0, "screenshot": "not_image.jpg"})
    belt = _belt(report_dir, report)
    escaped = json.loads(belt.execute("view_frame", {"finding_id": 1}))
    assert escaped["attached"] == 0
    errors = [row["error"] for row in escaped["frames"]]
    assert errors == [
        "frame path is not report-relative",
        "frame file is missing from the report directory",
    ]
    junk = json.loads(belt.execute("view_frame", {"finding_id": 2}))
    assert junk["frames"][0]["error"] == "stored file is not a JPEG or PNG image"
    assert belt.drain_images() == []


def test_view_frame_without_report_dir_is_explicit() -> None:
    result = json.loads(ReportToolbelt(_report()).execute("view_frame", {"finding_id": 1}))
    assert "report directory" in result["error"]


def test_view_frame_turn_budget_caps_attached_images(report_dir: Path) -> None:
    belt = _belt(report_dir)
    for _ in range(4):
        belt.execute("view_frame", {"finding_id": 1})
    capped = json.loads(belt.execute("view_frame", {"finding_id": 1}))
    assert capped["attached"] == 0
    assert "budget" in capped["frames"][0]["error"]
    assert len(belt.drain_images()) == 8


def test_seed_and_prompt_point_the_model_at_frames(report_dir: Path) -> None:
    seed = json.loads(seed_report_context(_report()))
    assert seed["findings"][0]["screenshot"] == "01_bug_09-22.jpg"
    assert seed["findings"][0]["merged_frames"] == 1
    turn = prepare_turn(report=_report(), message="co widać na klatce?")
    assert "view_frame" in turn.instructions
    assert "get_finding" in turn.instructions


def _provider(protocol: str, host: str) -> AgentProvider:
    return AgentProvider(
        name="primary",
        protocol=protocol,
        key="fake",  # pragma: allowlist secret
        model="vision-model",
        url=f"https://{host}/v1/{'messages' if protocol == 'anthropic' else 'responses'}",
        trust="internal",
        slot="primary",
        host=host,
    )


def _run(provider: AgentProvider, belt: ReportToolbelt) -> tuple[list[dict[str, Any]], str]:
    payloads: list[dict[str, Any]] = []
    rounds = iter(
        [
            ProviderRound(
                text="",
                response_id="resp_1",
                function_calls=[
                    FunctionCall(name="view_frame", call_id="call_1", arguments={"finding_id": 1})
                ],
                tool_use_blocks=[
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "view_frame",
                        "input": {"finding_id": 1},
                    }
                ],
            ),
            ProviderRound(text="Widzę panel agenta.", response_id="resp_2", function_calls=[]),
        ]
    )

    async def fake_round(_provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
        payloads.append(json.loads(json.dumps(payload)))
        return next(rounds)

    turn = prepare_turn(report=belt.report, message="co jest na klatce 1?")

    async def _collect() -> str:
        return "".join(
            [
                frame
                async for frame in agent_chat._run_provider(
                    config=ScreenScribeConfig(api_key="test-key"),  # pragma: allowlist secret
                    provider=provider,
                    turn=turn,
                    tools=belt,
                )
            ]
        )

    original = agent_chat.round_tripper
    agent_chat.round_tripper = fake_round
    try:
        body = asyncio.run(_collect())
    finally:
        agent_chat.round_tripper = original
    return payloads, body


@pytest.mark.parametrize("host", ["api.libraxis.cloud", "api.x.ai"])
def test_responses_loop_sends_frames_as_input_image(report_dir: Path, host: str) -> None:
    payloads, body = _run(_provider("responses", host), _belt(report_dir))
    assert len(payloads) == 2
    image_messages = [
        item
        for item in payloads[1]["input"]
        if item.get("role") == "user"
        and any(block.get("type") == "input_image" for block in item.get("content", []))
    ]
    assert len(image_messages) == 1
    urls = [
        block["image_url"]
        for block in image_messages[0]["content"]
        if block.get("type") == "input_image"
    ]
    assert urls == [
        "data:image/jpeg;base64," + base64.b64encode(JPEG).decode(),
        "data:image/jpeg;base64," + base64.b64encode(JPEG + b"member").decode(),
    ]
    assert "not a user instruction" in image_messages[0]["content"][0]["text"]
    # The browser SSE stream carries frame metadata, never pixels.
    assert base64.b64encode(JPEG).decode() not in body
    assert "Widzę panel agenta." in body


def test_anthropic_loop_sends_frames_inside_tool_result(report_dir: Path) -> None:
    payloads, body = _run(_provider("anthropic", "proxy.example"), _belt(report_dir))
    tool_results = [
        block
        for item in payloads[1]["input"]
        if item.get("role") == "user" and isinstance(item.get("content"), list)
        for block in item["content"]
        if block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    content = tool_results[0]["content"]
    assert content[0]["type"] == "text"
    images = [block for block in content if block.get("type") == "image"]
    assert [base64.b64decode(img["source"]["data"]) for img in images] == [
        JPEG,
        JPEG + b"member",
    ]
    messages = agent_chat._anthropic_messages_from_input(payloads[1]["input"])
    forwarded = json.dumps(messages)
    assert '"type": "image"' in forwarded
    assert base64.b64encode(JPEG).decode() not in body
