"""Review-server agent routes: SSE contract + non-stream JSON (mocked provider)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.agent.chat import AgentChatError, AgentProvider, ProviderRound, format_sse
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app

FIXTURE = Path(__file__).parent / "fixtures" / "agent_report_2026-09-15.json"


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.x.ai/v1/responses",
        stt_endpoint="https://api.x.ai/v1/stt",
        vision_endpoint="https://api.x.ai/v1/responses",
        llm_model="grok-4.6",
    )


def _app(
    tmp_path: Path,
    repo_root: Path | None = None,
    *,
    include_processing_provenance: bool = True,
) -> Any:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42")
    json_path = output_dir / "screen_report.json"
    report = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if include_processing_provenance:
        report["processing_provenance"] = {
            "llm": {"host": "api.x.ai", "protocol": "responses", "provider": "xai"}
        }
    json_path.write_text(json.dumps(report), encoding="utf-8")
    return create_review_app(
        output_dir,
        report_file.name,
        video_path,
        _config(),
        repo_root=repo_root,
    )


async def _fake_stream(**_kwargs: Any) -> AsyncIterator[str]:
    yield format_sse("token", {"text": "HIGH: układ."})
    yield format_sse("done", {"response_id": "resp_mock"})


async def _fake_collect(**_kwargs: Any) -> dict[str, Any]:
    return {"text": "HIGH: układ.", "response_id": "resp_mock"}


def test_agent_chat_stream_default_config_does_not_egress_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "ANTHROPIC_API_KEY",
        "SCREENSCRIBE_AGENT_FALLBACK_API_KEY",
        "SCREENSCRIBE_AGENT_EGRESS",
        "SCREENSCRIBE_AGENT_PRIMARY_TRUST",
    ):
        monkeypatch.delenv(key, raising=False)

    async def fake_round(_provider: AgentProvider, _payload: dict[str, Any]) -> ProviderRound:
        return ProviderRound(text="HIGH: układ.", response_id="resp_default", function_calls=[])

    monkeypatch.setattr("screenscribe.agent.chat.round_tripper", fake_round)
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/agent/chat/stream",
        json={"message": "Które findings są krytyczne?", "history": []},
    )
    assert response.status_code == 200
    body = response.text
    assert "event: token\n" in body
    assert "HIGH: układ." in body
    assert "event: done\n" in body
    assert "egress" not in body.lower()


def test_agent_chat_legacy_report_without_receipt_is_external_under_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_round(_provider: AgentProvider, _payload: dict[str, Any]) -> ProviderRound:
        nonlocal called
        called = True
        return ProviderRound(text="unexpected", response_id=None, function_calls=[])

    monkeypatch.setattr("screenscribe.agent.chat.round_tripper", fake_round)
    client = TestClient(_app(tmp_path, include_processing_provenance=False))
    response = client.post(
        "/api/agent/chat/stream",
        json={"message": "hello", "history": []},
    )
    assert response.status_code == 200
    assert called is False
    assert "event: error" in response.text
    assert "matching successful semantic-LLM receipt" in response.text


def test_agent_chat_stream_sse_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("screenscribe.review_server.stream_agent_chat", _fake_stream)
    app = _app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/api/agent/chat/stream",
        json={"message": "Które findings są krytyczne?", "history": []},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.text
    assert "event: token\n" in body
    assert '"text": "HIGH: układ."' in body
    assert "event: done\n" in body
    assert '"response_id": "resp_mock"' in body


def test_agent_chat_route_forwards_cursor_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def capture_stream(**kwargs: Any) -> AsyncIterator[str]:
        captured.update(kwargs)
        yield format_sse(
            "done",
            {
                "response_id": "resp_next",
                "provider": "primary",
                "protocol": "responses",
                "host": "api.x.ai",
            },
        )

    monkeypatch.setattr("screenscribe.review_server.stream_agent_chat", capture_stream)
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/agent/chat/stream",
        json={
            "message": "continue",
            "history": [],
            "previous_response_id": "resp_before",
            "previous_response_provider": "primary",
            "previous_response_protocol": "responses",
            "previous_response_host": "api.x.ai",
        },
    )
    assert response.status_code == 200
    assert captured["previous_response_id"] == "resp_before"
    assert captured["previous_response_provider"] == "primary"
    assert captured["previous_response_protocol"] == "responses"
    assert captured["previous_response_host"] == "api.x.ai"


def test_agent_chat_routes_require_session_token_and_local_origin(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    missing = client.post(
        "/api/agent/chat",
        json={"message": "hi", "history": []},
        headers={"X-ScreenScribe-Token": ""},
    )
    assert missing.status_code == 403

    foreign_origin = client.post(
        "/api/agent/chat/stream",
        json={"message": "hi", "history": []},
        headers={
            "X-ScreenScribe-Token": app.state.session_token,
            "Origin": "https://evil.example",
        },
    )
    assert foreign_origin.status_code == 403


def test_agent_chat_non_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("screenscribe.review_server.collect_agent_chat", _fake_collect)
    app = _app(tmp_path)
    client = TestClient(app)
    response = client.post(
        "/api/agent/chat",
        json={"message": "Które findings są krytyczne?", "history": []},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "HIGH: układ."
    assert payload["response_id"] == "resp_mock"


def test_agent_chat_non_stream_does_not_expose_exception_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_detail = f"debug path: {tmp_path / 'private-source.py'}"

    async def fail_collect(**_kwargs: Any) -> dict[str, Any]:
        raise AgentChatError(private_detail)

    monkeypatch.setattr("screenscribe.review_server.collect_agent_chat", fail_collect)
    response = TestClient(_app(tmp_path)).post("/api/agent/chat", json={"message": "hi"})
    assert response.status_code == 502
    assert response.json() == {"detail": "Review agent request failed. Check server logs."}
    assert private_detail not in response.text
    assert str(tmp_path) not in response.text


def test_agent_chat_rejects_empty_message(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    response = client.post("/api/agent/chat", json={"message": "", "history": []})
    assert response.status_code == 422


def test_agent_chat_rejects_oversized_message_and_history(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    oversized_message = client.post(
        "/api/agent/chat",
        json={"message": "x" * 16_001, "history": []},
    )
    assert oversized_message.status_code == 422

    oversized_history = client.post(
        "/api/agent/chat",
        json={
            "message": "hi",
            "history": [{"role": "user", "content": "x" * 16_001}],
        },
    )
    assert oversized_history.status_code == 422

    too_many_history_items = client.post(
        "/api/agent/chat",
        json={
            "message": "hi",
            "history": [{"role": "user", "content": "x"}] * 51,
        },
    )
    assert too_many_history_items.status_code == 422

    oversized_identity = client.post(
        "/api/agent/chat",
        json={
            "message": "hi",
            "history": [],
            "previous_response_id": "resp",
            "previous_response_provider": "p" * 33,
            "previous_response_protocol": "responses",
            "previous_response_host": "h" * 256,
        },
    )
    assert oversized_identity.status_code == 422


def test_agent_chat_404_without_report(tmp_path: Path) -> None:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"x")
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    response = client.post("/api/agent/chat", json={"message": "hi", "history": []})
    assert response.status_code == 404


def test_agent_chat_stream_tool_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def with_tools(**_kwargs: Any) -> AsyncIterator[str]:
        yield format_sse("tool_call", {"name": "list_findings", "input": {"filter": "high"}})
        yield format_sse("tool_result", {"name": "list_findings", "result": {"count": 2}})
        yield format_sse("token", {"text": "dwa high"})
        yield format_sse("done", {"response_id": "resp_tools"})

    monkeypatch.setattr("screenscribe.review_server.stream_agent_chat", with_tools)
    client = TestClient(_app(tmp_path))
    body = client.post(
        "/api/agent/chat/stream",
        json={"message": "high?", "history": [], "previous_response_id": None},
    ).text
    assert "event: tool_call\n" in body
    assert "event: tool_result\n" in body
    parsed = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
    names = [item.get("name") for item in parsed if "name" in item]
    assert "list_findings" in names
