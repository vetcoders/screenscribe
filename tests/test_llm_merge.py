"""Tests for the semantic LLM-merge pass (llm_merge_findings).

This pass runs AFTER the cheap heuristic `deduplicate_findings` and asks the
configured LLM to collapse cross-category PARAPHRASES of the same concrete issue
that string-similarity is blind to. The tests use a mock LLM caller (NO real
API) so the prompt-build + parse + merge mechanism is exercised deterministically.

Critical falsify (anti-over-merge): two distinct topics MUST stay two.
"""

from __future__ import annotations

import json as _json
from typing import Any, Literal

import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.unified.finding import UnifiedFinding
from screenscribe.unified.llm_merge import llm_merge_findings


def _mk(
    detection_id: int,
    summary: str,
    *,
    category: str = "ui",
    timestamp: float = 0.0,
    action_items: list[str] | None = None,
    affected_components: list[str] | None = None,
    severity: str = "medium",
    confidence: str = "high",
    parsed_from_unstructured_output: bool = False,
) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=detection_id,
        screenshot_path=None,
        timestamp=timestamp,
        category=category,
        is_issue=True,
        sentiment="neutral",
        severity=severity,
        summary=summary,
        action_items=action_items or [],
        affected_components=affected_components or [],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        confidence=confidence,
        parsed_from_unstructured_output=parsed_from_unstructured_output,
    )


def _cfg(*, enabled: bool = True, api_key: str = "test-key") -> ScreenScribeConfig:
    return ScreenScribeConfig(api_key=api_key, llm_merge_enabled=enabled)


def test_collapses_four_paraphrases_of_one_topic_into_one() -> None:
    """4 paraphrases of the SAME issue -> 1 merged finding (the core win)."""
    findings = [
        _mk(
            1,
            "The dropdown looks ugly",
            category="ui",
            timestamp=10,
            action_items=["Restyle the dropdown"],
            affected_components=["Dropdown"],
        ),
        _mk(
            2,
            "Dropdown styling is awkward and oversized",
            category="change",
            timestamp=22,
            action_items=["Reduce dropdown size"],
            affected_components=["Dropdown menu"],
        ),
        _mk(
            3,
            "The select control is not elegant",
            category="ui",
            timestamp=140,
            action_items=["Polish the select control"],
            affected_components=["Select"],
        ),
        _mk(
            4,
            "Make the dropdown nicer to look at please",
            category="other",
            timestamp=300,
            action_items=["Improve dropdown aesthetics"],
            affected_components=["Dropdown"],
        ),
    ]
    # Mock LLM groups all four as the same issue.
    merged = llm_merge_findings(
        findings, _cfg(), llm_caller=lambda _prompt: '{"groups": [[0, 1, 2, 3]]}'
    )
    assert len(merged) == 1
    one = merged[0]
    # merged_from_ids carries the provenance trail of the 3 merged-away members.
    assert len(one.merged_from_ids) == 3
    assert (2, 22.0) in one.merged_from_ids
    assert (4, 300.0) in one.merged_from_ids
    # Union of action_items preserved (no theme lost; uncapped union).
    assert set(one.action_items) == {
        "Restyle the dropdown",
        "Reduce dropdown size",
        "Polish the select control",
        "Improve dropdown aesthetics",
    }
    # Richest (longest) summary kept as the editable description.
    assert one.summary == max((f.summary for f in findings), key=len)


def test_distinct_topics_stay_separate_negative_anti_over_merge() -> None:
    """NEGATIVE (verifier-required): 2 different problems are NOT merged.

    A conservative model declines to group unrelated findings, so the pass leaves
    both standing -- the anti-over-merge falsify from the SCAFFOLD.
    """
    findings = [
        _mk(1, "The validating-configuration warning is confusing", category="bug", timestamp=46),
        _mk(2, "The transcript is shown twice on the page", category="ui", timestamp=85),
    ]
    merged = llm_merge_findings(findings, _cfg(), llm_caller=lambda _prompt: '{"groups": []}')
    assert len(merged) == 2
    assert [f.summary for f in merged] == [f.summary for f in findings]


def test_over_eager_singleton_or_out_of_range_groups_are_ignored() -> None:
    """A malformed/over-eager response cannot double-merge or merge phantoms."""
    findings = [_mk(1, "topic A"), _mk(2, "topic B"), _mk(3, "topic C")]
    # Singleton group [0] is meaningless; index 9 is out of range; [0,0] dupes.
    merged = llm_merge_findings(
        findings,
        _cfg(),
        llm_caller=lambda _prompt: '{"groups": [[0], [9], [0, 0]]}',
    )
    assert len(merged) == 3


def test_disabled_flag_is_noop() -> None:
    """Flag off -> pass is a no-op even if the caller would merge everything."""
    findings = [_mk(1, "same", timestamp=1), _mk(2, "same", timestamp=2)]
    merged = llm_merge_findings(
        findings, _cfg(enabled=False), llm_caller=lambda _prompt: '{"groups": [[0, 1]]}'
    )
    assert merged is findings


def test_missing_api_key_is_noop_without_network() -> None:
    """No LLM key + no injected caller -> safe no-op (fallback), no network call."""
    findings = [_mk(1, "a", timestamp=1), _mk(2, "b", timestamp=2)]
    merged = llm_merge_findings(findings, _cfg(api_key=""))  # no llm_caller
    assert merged == findings


def test_transport_failure_degrades_to_noop() -> None:
    """A raising caller (provider/transport error) -> heuristic result unchanged."""
    findings = [_mk(1, "x", timestamp=1), _mk(2, "y", timestamp=2)]

    def boom(_prompt: str) -> str:
        raise RuntimeError("provider exploded")

    merged = llm_merge_findings(findings, _cfg(), llm_caller=boom)
    assert merged == findings


def test_merge_is_conservative_chain_preserves_prior_provenance() -> None:
    """Merging a finding that ALREADY carries merged_from_ids keeps the full trail."""
    a = _mk(1, "first", timestamp=1)
    a.merged_from_ids = [(99, 0.5)]  # provenance from a prior (heuristic) merge
    b = _mk(2, "second longer summary", timestamp=2)
    merged = llm_merge_findings([a, b], _cfg(), llm_caller=lambda _prompt: '{"groups": [[0, 1]]}')
    assert len(merged) == 1
    ids = merged[0].merged_from_ids
    assert (99, 0.5) in ids  # prior trail survives the second pass
    assert (2, 2.0) in ids


# --- Real transport: reasoning effort + failed/incomplete 200 bodies ----------


def _merge_client(payload: dict[str, Any], bodies: list[dict[str, Any]]) -> type:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return payload

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            return False

        def post(self, *args: Any, **kwargs: Any) -> _Response:
            bodies.append(kwargs.get("json") or {})
            return _Response()

    return _Client


def _groups_payload(status: str = "completed", **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        **extra,
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": '{"groups": []}'}]},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": _json.dumps({"groups": [[0, 1]]})}],
            },
        ],
    }


def _two_paraphrases() -> list[UnifiedFinding]:
    return [
        _mk(1, "Save button does nothing when clicked", timestamp=1.0),
        _mk(2, "Clicking save has no effect", timestamp=2.0),
    ]


@pytest.mark.parametrize(
    ("endpoint", "effort", "expected_reasoning"),
    [
        (None, None, {"summary": "auto", "effort": "medium"}),
        (None, "low", {"summary": "auto", "effort": "low"}),
        ("https://api.example.com/v1/chat/completions", "low", None),
    ],
)
def test_default_caller_sends_configured_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str | None,
    effort: str | None,
    expected_reasoning: dict[str, str] | None,
) -> None:
    config = _cfg()
    if endpoint:
        config.llm_endpoint = endpoint
    if effort:
        config.llm_reasoning_effort = effort
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "screenscribe.unified.llm_merge.httpx.Client", _merge_client(_groups_payload(), bodies)
    )

    llm_merge_findings(_two_paraphrases(), config)

    assert len(bodies) == 1
    assert bodies[0].get("reasoning") == expected_reasoning


def test_default_caller_merges_from_answer_not_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """``output[0]`` is a reasoning item whose summary text is also JSON; only the
    message's output_text may be parsed as the answer."""
    monkeypatch.setattr(
        "screenscribe.unified.llm_merge.httpx.Client", _merge_client(_groups_payload(), [])
    )

    merged = llm_merge_findings(_two_paraphrases(), _cfg())

    assert len(merged) == 1


@pytest.mark.parametrize(
    "payload",
    [
        _groups_payload("failed", error={"code": "server_error", "message": "rejected"}),
        _groups_payload("incomplete", incomplete_details={"reason": "max_output_tokens"}),
    ],
)
def test_default_caller_failed_or_incomplete_body_skips_merge(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    """A 200 body with status failed/incomplete is not an answer: merge is skipped."""
    findings = _two_paraphrases()
    monkeypatch.setattr("screenscribe.unified.llm_merge.httpx.Client", _merge_client(payload, []))
    monkeypatch.setattr("screenscribe.api_utils.time.sleep", lambda _d: None)

    merged = llm_merge_findings(findings, _cfg())

    assert merged == findings
