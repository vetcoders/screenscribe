"""Review-patch write tools: validate, return a patch, never write report.json."""

# ruff: noqa: S106 -- fixture tokens are literal test strings, not credentials
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from screenscribe.account_auth import AccountTokens, store_account_tokens
from screenscribe.agent.chat import (
    AgentProvider,
    FunctionCall,
    ProviderRound,
    build_providers,
    stream_agent_chat,
)
from screenscribe.agent.context import prepare_turn
from screenscribe.agent.tools import (
    SEVERITY_VALUES,
    WRITE_TOOL_NAMES,
    ReportToolbelt,
    anthropic_tool_schemas,
    responses_tool_schemas,
)
from screenscribe.config import ScreenScribeConfig
from screenscribe.work_item import VERDICT_VALUES

FIXTURE = Path(__file__).parent / "fixtures" / "agent_report_2026-09-15.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _toolbelt() -> tuple[ReportToolbelt, dict[str, Any]]:
    report = _load_fixture()
    return ReportToolbelt(report), report


def _xai_config(**overrides: Any) -> ScreenScribeConfig:
    values: dict[str, Any] = {
        "api_key": "test-key",  # pragma: allowlist secret
        "llm_endpoint": "https://api.x.ai/v1/responses",
        "stt_endpoint": "https://api.x.ai/v1/stt",
        "vision_endpoint": "https://api.x.ai/v1/responses",
        "llm_model": "grok-4.6",
        "agent_egress": "deny",
    }
    values.update(overrides)
    return ScreenScribeConfig(**values)


def _drop_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ANTHROPIC_API_KEY",
        "SCREENSCRIBE_AGENT_FALLBACK_API_KEY",
        "SCREENSCRIBE_AGENT_FALLBACK_URL",
        "SCREENSCRIBE_AGENT_FALLBACK_TRUST",
        "SCREENSCRIBE_AGENT_FALLBACK_PROTOCOL",
        "SCREENSCRIBE_AGENT_FALLBACK_MODEL",
        "SCREENSCRIBE_AGENT_PRIMARY_TRUST",
        "SCREENSCRIBE_AGENT_EGRESS",
        "SCREENSCRIBE_API_KEY",
        "SCREENSCRIBE_LLM_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_set_verdict_returns_review_patch() -> None:
    belt, report = _toolbelt()
    before = copy.deepcopy(report)
    result = belt.set_verdict(3, "accepted")
    assert result == {
        "type": "review_patch",
        "ops": [{"op": "set_verdict", "finding_id": "3", "verdict": "accepted"}],
        "explain": "Set finding 3 verdict to accepted.",
    }
    assert report == before


def test_set_verdict_unknown_finding_errors() -> None:
    result = _toolbelt()[0].set_verdict("999", "accepted")
    assert result["error"] == "Unknown finding_id: 999"


def test_set_verdict_invalid_value_lists_allowed() -> None:
    result = _toolbelt()[0].set_verdict(3, "ship-it")
    assert "error" in result
    for allowed in VERDICT_VALUES:
        assert allowed in result["error"]


def test_set_severity_returns_review_patch() -> None:
    result = _toolbelt()[0].set_severity("3", "high")
    assert result["type"] == "review_patch"
    assert result["ops"] == [{"op": "set_severity", "finding_id": "3", "severity": "high"}]


def test_set_severity_invalid_value_lists_allowed() -> None:
    result = _toolbelt()[0].set_severity(3, "urgent")
    assert "error" in result
    for allowed in SEVERITY_VALUES:
        assert allowed in result["error"]


def test_edit_finding_maps_overrides() -> None:
    result = _toolbelt()[0].edit_finding(
        3,
        {
            "summary": "Safari clip",
            "category": "ui",
            "notes": "sprawdzić na Safari",
            "action_items": ["otworzyć Safari", "porównać layout"],
        },
    )
    assert result["type"] == "review_patch"
    assert result["ops"] == [
        {
            "op": "edit_finding",
            "finding_id": "3",
            "fields": {
                "summary_override": "Safari clip",
                "category_override": "ui",
                "notes": "sprawdzić na Safari",
                "action_items": "otworzyć Safari\nporównać layout",
            },
        }
    ]


def test_edit_finding_nested_fields_and_unknown_id() -> None:
    belt = _toolbelt()[0]
    nested = belt.edit_finding("3", {"fields": {"notes": "only notes"}})
    assert nested["ops"][0]["fields"] == {"notes": "only notes"}
    missing = belt.edit_finding("nope", {"notes": "x"})
    assert missing["error"] == "Unknown finding_id: nope"
    empty = belt.edit_finding(3, {})
    assert "error" in empty


def test_add_finding_returns_patch_without_extracting_a_frame() -> None:
    belt, report = _toolbelt()
    before = copy.deepcopy(report)
    result = belt.add_finding(12.5, "Safari overlay", "high", "bug")
    assert result == {
        "type": "review_patch",
        "ops": [
            {
                "op": "add_finding",
                "timestamp": 12.5,
                "summary": "Safari overlay",
                "severity": "high",
                "category": "bug",
            }
        ],
        "explain": "Add a manual finding at 12.5s.",
    }
    assert report == before
    assert len(report["findings"]) == 13


def test_add_finding_rejects_bad_severity() -> None:
    result = _toolbelt()[0].add_finding(1.0, "x", "nope", "bug")
    assert "error" in result
    assert "critical" in result["error"]


def test_merge_and_unmerge_are_stubs() -> None:
    belt = _toolbelt()[0]
    merged = belt.merge_findings("3", ["8", "16"])
    unmerged = belt.unmerge_finding("3")
    assert merged["unsupported"] is True
    assert unmerged["unsupported"] is True
    parsed = json.loads(belt.execute("merge_findings", {"survivor_id": "3", "member_ids": ["8"]}))
    assert parsed["unsupported"] is True


def test_propose_review_returns_plan_and_applies_nothing() -> None:
    belt, report = _toolbelt()
    before = copy.deepcopy(report)
    result = belt.propose_review(
        "zmień finding 3 na high i dopisz notatkę",
        [
            {"op": "set_severity", "finding_id": "3", "severity": "high"},
            {"op": "edit_finding", "finding_id": 3, "fields": {"notes": "sprawdzić na Safari"}},
        ],
        ["raise severity", "ask for Safari check"],
    )
    assert result["type"] == "review_plan"
    assert result["ops"] == [
        {"op": "set_severity", "finding_id": "3", "severity": "high"},
        {
            "op": "edit_finding",
            "finding_id": "3",
            "fields": {"notes": "sprawdzić na Safari"},
        },
    ]
    assert result["rationale"] == ["raise severity", "ask for Safari check"]
    assert report == before


def test_propose_review_validates_ops_and_does_not_partial_apply() -> None:
    result = _toolbelt()[0].propose_review(
        "zmień wszystko",
        [
            {"op": "set_verdict", "finding_id": "3", "verdict": "accepted"},
            {"op": "set_verdict", "finding_id": "999", "verdict": "accepted"},
        ],
        ["ok", "bad"],
    )
    assert result["error"] == "Unknown finding_id: 999"
    assert "ops" not in result


def test_tool_schemas_list_write_tools() -> None:
    responses = responses_tool_schemas(include_repo=False)
    anthropic = anthropic_tool_schemas(include_repo=False)
    response_names = {tool["name"] for tool in responses}
    anthropic_names = {tool["name"] for tool in anthropic}
    for name in WRITE_TOOL_NAMES:
        assert name in response_names
        assert name in anthropic_names
    set_severity = next(tool for tool in responses if tool["name"] == "set_severity")
    assert set_severity["parameters"]["required"] == ["finding_id", "severity"]
    anth_severity = next(tool for tool in anthropic if tool["name"] == "set_severity")
    assert anth_severity["input_schema"] == set_severity["parameters"]


def test_system_prompt_directs_patches_and_propose_review() -> None:
    turn = prepare_turn(report=_load_fixture(), message="ustaw 3 na high")
    text = turn.instructions
    assert "propose_review" in text
    assert "review_applied" in text
    assert "set_verdict" in text
    assert "Never claim an edit is saved" in text or "never claim an edit is saved" in text.lower()


def test_stream_set_severity_round_trips_patch_in_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = iter(
        [
            ProviderRound(
                text="",
                response_id="resp_tools",
                function_calls=[
                    FunctionCall(
                        name="set_severity",
                        call_id="call_1",
                        arguments={"finding_id": "3", "severity": "high"},
                    )
                ],
            ),
            ProviderRound(text="Gotowe.", response_id="resp_final", function_calls=[]),
        ]
    )

    async def fake_round(_provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
        names = {tool["name"] for tool in payload.get("tools") or []}
        assert "set_severity" in names
        return next(rounds)

    monkeypatch.setattr("screenscribe.agent.chat.round_tripper", fake_round)
    _drop_agent_env(monkeypatch)
    config = _xai_config()
    report = _load_fixture()

    async def _collect() -> str:
        return "".join(
            [
                frame
                async for frame in stream_agent_chat(
                    config=config,
                    report=report,
                    tools=ReportToolbelt(report),
                    message="ustaw finding 3 na high",
                )
            ]
        )

    body = asyncio.run(_collect())
    assert "event: tool_call" in body
    assert "set_severity" in body
    assert "event: tool_result" in body
    payloads = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
    results = [item["result"] for item in payloads if "result" in item]
    assert results
    patch = results[0]
    assert patch["type"] == "review_patch"
    assert patch["ops"] == [{"op": "set_severity", "finding_id": "3", "severity": "high"}]
    assert '"response_id": "resp_final"' in body


def test_agent_uses_account_bearer_when_api_key_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drop_agent_env(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ScreenScribeConfig._account_warned.clear()
    store_account_tokens("xai", AccountTokens(access_token="at-account-xai", expires_at=None))
    config = _xai_config(api_key="", llm_api_key="")
    assert config.get_llm_api_key() == "at-account-xai"
    providers = build_providers(config)
    assert providers
    assert providers[0].key == "at-account-xai"
    assert providers[0].host == "api.x.ai"


def test_execute_json_round_trip_does_not_mutate_report() -> None:
    belt, report = _toolbelt()
    before = copy.deepcopy(report)
    payload = json.loads(
        belt.execute(
            "edit_finding",
            {"finding_id": 3, "notes": "sprawdzić na Safari", "summary": "Safari"},
        )
    )
    assert payload["type"] == "review_patch"
    assert payload["ops"][0]["fields"]["notes"] == "sprawdzić na Safari"
    assert payload["ops"][0]["fields"]["summary_override"] == "Safari"
    assert report == before
