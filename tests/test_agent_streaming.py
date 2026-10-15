"""Agent turns stream provider deltas live; keepalives; cancellation; dictation STT."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import screenscribe.agent.chat as agent_chat
from screenscribe.agent.chat import (
    KEEPALIVE_FRAME,
    AgentProvider,
    ProviderRound,
    _parse_sse_frame,
    stream_agent_chat,
)
from screenscribe.agent.context import prepare_turn
from screenscribe.agent.tools import ReportToolbelt
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app
from screenscribe.transcribe import Segment, TranscriptionResult

VALID_BROWSER_AUDIO = b"voice-data" + (b"x" * 2048)


def _provider(slot: str = "primary", host: str = "api.libraxis.cloud") -> AgentProvider:
    return AgentProvider(
        name=slot,
        protocol="responses",
        key="fake",  # pragma: allowlist secret
        model="m",
        url=f"https://{host}/v1/responses",
        trust="internal",
        slot=slot,
        host=host,
    )


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(api_key="test-key")  # pragma: allowlist secret


async def _frames_with_marks(
    provider: AgentProvider, marks: list[str]
) -> list[tuple[str, list[str]]]:
    """Collect frames, recording which provider progress marks existed at each."""
    report = {"findings": []}
    turn = prepare_turn(report=report, message="hi")
    out: list[tuple[str, list[str]]] = []
    async for frame in agent_chat._run_provider(
        config=_config(), provider=provider, turn=turn, tools=ReportToolbelt(report)
    ):
        out.append((frame, list(marks)))
    return out


def test_tokens_reach_the_client_before_the_round_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marks: list[str] = []

    async def fake_round(
        _provider: AgentProvider, _payload: dict[str, Any], on_delta: Any = None
    ) -> ProviderRound:
        for piece in ("Pierwsze ", "zdanie ", "odpowiedzi."):
            on_delta(piece)
            marks.append(piece)
            await asyncio.sleep(0.02)
        marks.append("round-finished")
        return ProviderRound(
            text="Pierwsze zdanie odpowiedzi.", response_id="r1", function_calls=[]
        )

    monkeypatch.setattr(agent_chat, "_responses_round", fake_round)
    frames = asyncio.run(_frames_with_marks(_provider(), marks))
    tokens = [(f, m) for f, m in frames if f.startswith("event: token")]
    assert [_parse_sse_frame(f)[1]["text"] for f, _m in tokens] == [
        "Pierwsze ",
        "zdanie ",
        "odpowiedzi.",
    ]
    # The first token frame was yielded while the provider was still generating.
    assert "round-finished" not in tokens[0][1]
    # No duplicate full-text frame after the deltas.
    assert sum(1 for f, _m in frames if "Pierwsze zdanie odpowiedzi." in f) == 0
    assert frames[-1][0].startswith("event: done")


def test_round_without_deltas_still_sends_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_round(
        _provider: AgentProvider, _payload: dict[str, Any], on_delta: Any = None
    ) -> ProviderRound:
        return ProviderRound(text="Cała odpowiedź.", response_id="r1", function_calls=[])

    monkeypatch.setattr(agent_chat, "_responses_round", fake_round)
    frames = asyncio.run(_frames_with_marks(_provider(), []))
    tokens = [_parse_sse_frame(f)[1]["text"] for f, _m in frames if f.startswith("event: token")]
    assert tokens == ["Cała odpowiedź."]


def test_silent_provider_gets_keepalive_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_chat, "_KEEPALIVE_SECONDS", 0.02)

    async def fake_round(
        _provider: AgentProvider, _payload: dict[str, Any], on_delta: Any = None
    ) -> ProviderRound:
        await asyncio.sleep(0.12)
        on_delta("ok")
        return ProviderRound(text="ok", response_id="r1", function_calls=[])

    monkeypatch.setattr(agent_chat, "_responses_round", fake_round)
    frames = [f for f, _m in asyncio.run(_frames_with_marks(_provider(), []))]
    first_token = next(i for i, f in enumerate(frames) if f.startswith("event: token"))
    assert KEEPALIVE_FRAME in frames[:first_token]
    assert _parse_sse_frame(KEEPALIVE_FRAME) == ("", {})


def test_keepalive_only_primary_failure_still_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_chat, "_KEEPALIVE_SECONDS", 0.02)
    calls: list[str] = []

    async def fake_dispatch(
        provider: AgentProvider, _payload: dict[str, Any], on_delta: Any = None
    ) -> ProviderRound:
        calls.append(provider.slot)
        if provider.slot == "primary":
            await asyncio.sleep(0.08)
            raise agent_chat.AgentChatError("primary down")
        on_delta("fallback answer")
        return ProviderRound(text="fallback answer", response_id=None, function_calls=[])

    monkeypatch.setattr(agent_chat, "_dispatch_round", fake_dispatch)
    monkeypatch.setattr(
        agent_chat,
        "build_providers",
        lambda *_a, **_k: [_provider("primary"), _provider("fallback", "fallback.example")],
    )
    monkeypatch.setattr(agent_chat, "apply_egress", lambda providers, _egress: (providers, []))

    async def collect() -> str:
        return "".join(
            [
                frame
                async for frame in stream_agent_chat(
                    config=_config(),
                    report={"findings": []},
                    tools=ReportToolbelt({"findings": []}),
                    message="hi",
                )
            ]
        )

    body = asyncio.run(collect())
    assert calls == ["primary", "fallback"]
    assert "fallback answer" in body
    assert "event: error" not in body


def test_closing_the_stream_cancels_the_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, bool] = {"cancelled": False, "finished": False}

    async def fake_round(
        _provider: AgentProvider, _payload: dict[str, Any], on_delta: Any = None
    ) -> ProviderRound:
        on_delta("partial")
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        state["finished"] = True
        return ProviderRound(text="partial", response_id=None, function_calls=[])

    monkeypatch.setattr(agent_chat, "_responses_round", fake_round)

    async def run() -> list[str]:
        report = {"findings": []}
        turn = prepare_turn(report=report, message="hi")
        gen = agent_chat._run_provider(
            config=_config(), provider=_provider(), turn=turn, tools=ReportToolbelt(report)
        )
        first = await gen.__anext__()
        await gen.aclose()  # what Starlette does when the browser disconnects
        await asyncio.sleep(0)
        return [first]

    frames = asyncio.run(run())
    assert json.loads(frames[0].split("data: ", 1)[1])["text"] == "partial"
    assert state == {"cancelled": True, "finished": False}


def test_responses_round_reports_each_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_text.delta", "delta": "Ala "},
        {"type": "response.output_text.delta", "delta": "ma kota"},
        {"type": "response.output_text.done", "text": "Ala ma kota"},
    ]

    class FakeResponse:
        status_code = 200

        async def __aenter__(self) -> FakeResponse:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def aiter_lines(self) -> Any:
            for event in events:
                yield f"data: {json.dumps(event)}"

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def stream(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("screenscribe.agent.chat.httpx.AsyncClient", FakeClient)
    seen: list[str] = []
    result = asyncio.run(agent_chat._responses_round(_provider(), {}, on_delta=seen.append))
    assert seen == ["Ala ", "ma kota"]
    assert result.text == "Ala ma kota"


# --- /api/agent/stt ------------------------------------------------------------


@pytest.fixture
def review_app(tmp_path: Path) -> Any:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    config = ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        llm_model="programmer",
        vision_model="programmer",
    )
    return create_review_app(output_dir, report_file.name, video_path, config)


def _review_session(app: Any) -> Any:
    from screenscribe import review_server as mod

    for route in app.routes:
        closure = getattr(getattr(route, "endpoint", None), "__closure__", None) or ()
        for cell in closure:
            if isinstance(cell.cell_contents, mod.ReviewSession):
                return cell.cell_contents
    raise AssertionError("could not reach ReviewSession")


def test_agent_dictation_transcribes_without_touching_the_review_chain(
    monkeypatch: pytest.MonkeyPatch, review_app: Any
) -> None:
    def fake_transcribe(*args: Any, **kwargs: Any) -> TranscriptionResult:
        assert args[0] == VALID_BROWSER_AUDIO
        assert kwargs["content_type"] == "audio/webm"
        return TranscriptionResult(
            text="Sprawdź klatkę z dwunastej minuty",
            segments=[Segment(id=1, start=0.0, end=2.0, text="Sprawdź klatkę")],
            language="pl",
            response_id="resp_dictation",
        )

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe)
    session = _review_session(review_app)
    before = session.last_response_id
    response = TestClient(review_app).post(
        "/api/agent/stt",
        files={"audio": ("dictation.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "Sprawdź klatkę z dwunastej minuty"
    assert session.last_response_id == before


@pytest.mark.parametrize(
    ("payload", "status", "detail"),
    [
        (b"", 400, "Voice recording is empty."),
        (b"x" * 10, 400, "Voice recording is too short. Hold to record longer."),
    ],
)
def test_agent_dictation_rejects_empty_and_tiny_audio(
    review_app: Any, payload: bytes, status: int, detail: str
) -> None:
    response = TestClient(review_app).post(
        "/api/agent/stt", files={"audio": ("dictation.webm", payload, "audio/webm")}
    )
    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_agent_dictation_shares_the_declared_upload_cap(review_app: Any) -> None:
    from screenscribe.review_server import MAX_AUDIO_BYTES

    response = TestClient(review_app).post(
        "/api/agent/stt",
        content=b"x",
        headers={
            "content-type": "audio/webm",
            "content-length": str(MAX_AUDIO_BYTES + 64 * 1024 + 1),
        },
    )
    assert response.status_code == 413


def test_agent_dictation_failure_hides_provider_detail(
    monkeypatch: pytest.MonkeyPatch, review_app: Any
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> TranscriptionResult:
        raise RuntimeError("secret upstream detail sk-123")

    monkeypatch.setattr("screenscribe.server_common.transcribe_browser_audio", boom)
    monkeypatch.setattr("screenscribe.review_server.transcribe_browser_audio", boom)
    response = TestClient(review_app).post(
        "/api/agent/stt",
        files={"audio": ("dictation.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )
    assert response.status_code == 502
    assert "sk-123" not in response.text


def test_agent_stream_response_disables_buffering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_stream(**_kwargs: Any) -> Any:
        yield 'event: token\ndata: {"text": "hi"}\n\n'

    monkeypatch.setattr("screenscribe.review_server.stream_agent_chat", fake_stream)
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    (output_dir / "screen_report.html").write_text("<html></html>", encoding="utf-8")
    (output_dir / "screen_report.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42")
    app = create_review_app(output_dir, "screen_report.html", video_path, _config())
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/agent/chat/stream",
        json={"message": "hi"},
        headers={"X-ScreenScribe-Token": app.state.session_token},
    ) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert "hi" in "".join(response.iter_text())
