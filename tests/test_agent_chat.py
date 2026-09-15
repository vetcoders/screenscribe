"""Review-agent chat: chain head, seed, egress, tool loop, report response_id."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from screenscribe.agent.chat import (
    AgentChatError,
    AgentProvider,
    FunctionCall,
    ProviderRound,
    apply_egress,
    build_providers,
    collect_agent_chat,
    format_sse,
    stream_agent_chat,
)
from screenscribe.agent.context import prepare_turn, report_chain_response_id
from screenscribe.agent.tools import ReportToolbelt
from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.report import save_enhanced_json_report
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import UnifiedFinding

FIXTURE = Path(__file__).parent / "fixtures" / "agent_report_2026-09-15.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _xai_config(**overrides: Any) -> ScreenScribeConfig:
    values: dict[str, Any] = {
        "api_key": "test-key",  # pragma: allowlist secret
        "llm_endpoint": "https://api.x.ai/v1/responses",
        "llm_model": "grok-4.6",
        "agent_egress": "deny",
    }
    values.update(overrides)
    return ScreenScribeConfig(**values)


def test_format_sse_named_event_contract() -> None:
    frame = format_sse("token", {"text": "hi"})
    assert frame.startswith("event: token\n")
    assert 'data: {"text": "hi"}' in frame
    assert frame.endswith("\n\n")


def test_report_chain_id_falls_back_to_last_finding() -> None:
    report = _load_fixture()
    assert report["analysis_passes"]["unified_analysis"].get("response_id") in (None, "")
    chained = report_chain_response_id(report)
    assert chained
    last_finding = report["findings"][-1]
    assert chained == last_finding["unified_analysis"]["response_id"]


def test_prepare_turn_uses_request_previous_response_id() -> None:
    report = _load_fixture()
    turn = prepare_turn(
        report=report,
        message="co jest krytyczne?",
        previous_response_id="resp_from_client",
    )
    assert turn.previous_response_id == "resp_from_client"
    assert turn.seeded is False
    assert "executive_summary" not in turn.instructions


def test_prepare_turn_uses_report_chain_when_request_omits_id() -> None:
    report = _load_fixture()
    turn = prepare_turn(report=report, message="co jest krytyczne?")
    assert turn.previous_response_id == report_chain_response_id(report)
    assert turn.seeded is False


def test_prepare_turn_seeds_json_when_no_chain_id() -> None:
    report = {
        "executive_summary": "Layout is broken.",
        "findings": [{"id": 1, "category": "bug", "text": "save fails"}],
        "transcript_segments": [{"id": 0, "start": 1.0, "end": 2.0, "text": "hello"}],
        "analysis_passes": {"unified_analysis": {"status": "completed", "count": 1}},
    }
    turn = prepare_turn(report=report, message="podsumuj")
    assert turn.previous_response_id is None
    assert turn.seeded is True
    assert "Layout is broken." in turn.instructions
    assert "save fails" in turn.instructions or "1" in turn.instructions


def test_egress_deny_skips_external_xai() -> None:
    config = _xai_config(agent_egress="deny")
    providers = build_providers(config)
    assert providers[0].trust == "external"
    kept, skipped = apply_egress(providers, config.agent_egress)
    assert kept == []
    assert skipped and skipped[0].host == "api.x.ai"


def test_egress_allow_keeps_external_xai() -> None:
    config = _xai_config(agent_egress="allow")
    kept, skipped = apply_egress(build_providers(config), config.agent_egress)
    assert skipped == []
    assert kept[0].host == "api.x.ai"


def test_primary_trust_internal_survives_deny() -> None:
    config = _xai_config(agent_egress="deny", agent_primary_trust="internal")
    kept, skipped = apply_egress(build_providers(config), config.agent_egress)
    assert skipped == []
    assert kept[0].trust == "internal"


def test_stream_agent_chat_emits_token_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_round(_provider: AgentProvider, _payload: dict[str, Any]) -> ProviderRound:
        return ProviderRound(text="Krytyczne: layout.", response_id="resp_live", function_calls=[])

    monkeypatch.setattr("screenscribe.agent.chat.round_tripper", fake_round)
    config = _xai_config(agent_egress="allow")
    report = _load_fixture()

    async def _collect() -> str:
        frames = [
            frame
            async for frame in stream_agent_chat(
                config=config,
                report=report,
                tools=ReportToolbelt(report),
                message="Które findings są krytyczne?",
            )
        ]
        return "".join(frames)

    body = asyncio.run(_collect())
    assert "event: token" in body
    assert "Krytyczne: layout." in body
    assert "event: done" in body
    assert "resp_live" in body


def test_stream_agent_chat_runs_tool_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    rounds = iter(
        [
            ProviderRound(
                text="",
                response_id="resp_tools",
                function_calls=[
                    FunctionCall(name="get_report_summary", call_id="call_1", arguments={})
                ],
            ),
            ProviderRound(text="Podsumowanie gotowe.", response_id="resp_final", function_calls=[]),
        ]
    )

    async def fake_round(_provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
        _ = payload
        return next(rounds)

    monkeypatch.setattr("screenscribe.agent.chat.round_tripper", fake_round)
    config = _xai_config(agent_egress="allow")
    report = _load_fixture()

    async def _collect() -> str:
        return "".join(
            [
                frame
                async for frame in stream_agent_chat(
                    config=config,
                    report=report,
                    tools=ReportToolbelt(report),
                    message="podsumuj raport",
                )
            ]
        )

    body = asyncio.run(_collect())
    assert "event: tool_call" in body
    assert "get_report_summary" in body
    assert "event: tool_result" in body
    assert "event: token" in body
    assert "Podsumowanie gotowe." in body
    assert '"response_id": "resp_final"' in body


def test_collect_agent_chat_raises_when_egress_denies_all() -> None:
    config = _xai_config(agent_egress="deny")
    report = _load_fixture()

    async def _run() -> None:
        await collect_agent_chat(
            config=config,
            report=report,
            tools=ReportToolbelt(report),
            message="hi",
        )

    with pytest.raises(AgentChatError, match="egress"):
        asyncio.run(_run())


def test_pipeline_writes_last_pass_response_id(tmp_path: Path) -> None:
    first = Detection(
        segment=Segment(id=1, start=1.0, end=2.0, text="one"),
        category="bug",
        keywords_found=[],
        context="",
    )
    second = Detection(
        segment=Segment(id=2, start=3.0, end=4.0, text="two"),
        category="ui",
        keywords_found=[],
        context="",
    )
    shot_a = tmp_path / "a.jpg"
    shot_b = tmp_path / "b.jpg"
    shot_a.write_bytes(b"a")
    shot_b.write_bytes(b"b")

    def _finding(detection: Detection, response_id: str) -> UnifiedFinding:
        return UnifiedFinding(
            detection_id=detection.segment.id,
            screenshot_path=None,
            timestamp=detection.segment.start,
            category=detection.category,
            is_issue=True,
            sentiment="problem",
            severity="high",
            summary="issue",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id=response_id,
        )

    output = tmp_path / "report.json"
    save_enhanced_json_report(
        detections=[first, second],
        screenshots=[(first, shot_a), (second, shot_b)],
        video_path=tmp_path / "video.mov",
        output_path=output,
        unified_findings=[_finding(first, "resp_first"), _finding(second, "resp_last")],
        executive_summary="summary",
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["analysis_passes"]["unified_analysis"]["response_id"] == "resp_last"


def test_config_file_loads_agent_egress(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text(
        "SCREENSCRIBE_AGENT_EGRESS=allow\nSCREENSCRIBE_AGENT_PRIMARY_TRUST=internal\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    config = ScreenScribeConfig()
    config._load_from_file(path)
    assert config.agent_egress == "allow"
    assert config.agent_primary_trust == "internal"
