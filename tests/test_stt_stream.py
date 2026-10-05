"""Live STT websocket clients against a fake loopback server (no network).

The fake server records what the client sends and replays scripted frames the
way the real gateways do (LibraxisAI ``stt-ws-v1`` vocabulary from
``codescribe/core/asr_session/cloud.rs``; xAI events from docs.x.ai).
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from screenscribe.stt_stream import (
    LibraxisStreamClient,
    TranscriptEvent,
    XaiStreamClient,
    stream_client_for_endpoint,
)

Handler = Callable[[ServerConnection, dict[str, Any]], Coroutine[Any, Any, None]]


class _FakeServer:
    """Loopback websocket server driven from a background asyncio thread."""

    def __init__(self, handler: Handler) -> None:
        self.recorded: dict[str, Any] = {"text": [], "binary": [], "path": None, "headers": None}
        self._handler = handler
        self._port_ready = threading.Event()
        self._stop: asyncio.Future[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.port = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = self._loop.create_future()

        async def on_connect(ws: ServerConnection) -> None:
            self.recorded["path"] = ws.request.path if ws.request else None
            self.recorded["headers"] = (
                {"Authorization": ws.request.headers.get("Authorization")} if ws.request else None
            )
            await self._handler(ws, self.recorded)

        async with serve(on_connect, "127.0.0.1", 0) as server:
            self.port = next(iter(server.sockets)).getsockname()[1]
            self._port_ready.set()
            await self._stop

    def __enter__(self) -> _FakeServer:
        self._thread.start()
        assert self._port_ready.wait(5), "fake server did not start"
        return self

    def __exit__(self, *exc: object) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set_result, None)
        self._thread.join(5)


async def _collect_until(ws: ServerConnection, recorded: dict[str, Any], end_type: str) -> None:
    async for message in ws:
        if isinstance(message, bytes):
            recorded["binary"].append(message)
            continue
        recorded["text"].append(json.loads(message))
        if recorded["text"][-1].get("type") == end_type:
            return


async def _libraxis_handler(ws: ServerConnection, recorded: dict[str, Any]) -> None:
    await ws.send(json.dumps({"type": "hello", "protocol_version": 1}))
    await _collect_until(ws, recorded, "end")
    for frame in (
        {"type": "ack"},
        {"type": "vad.sample", "p": 0.9},
        {"type": "speech.start"},
        {"type": "transcript.partial", "text": "dzień"},
        {"type": "transcript.partial", "text": "  "},
        {"type": "transcript", "text": "dzień dob"},
        {
            "type": "transcript.final",
            "text": "dzień dobry",
            "utterance_id": 7,
            "start_ms": 120,
            "end_ms": 980,
        },
        {"type": "transcript.partial", "text": "kolejne"},
        {"type": "transcript.final", "text": "kolejne zdanie"},
        {"type": "session.ended"},
    ):
        await ws.send(json.dumps(frame))


async def _xai_handler(ws: ServerConnection, recorded: dict[str, Any]) -> None:
    await ws.send(json.dumps({"type": "transcript.created"}))
    await _collect_until(ws, recorded, "audio.done")
    for frame in (
        {
            "type": "transcript.partial",
            "text": "hello",
            "is_final": False,
            "speech_final": False,
            "start": 0.0,
            "duration": 0.4,
        },
        {
            "type": "transcript.partial",
            "text": "hello there",
            "is_final": True,
            "speech_final": False,
            "start": 0.0,
            "duration": 0.9,
        },
        {
            "type": "transcript.partial",
            "text": "hello there",
            "is_final": True,
            "speech_final": True,
            "start": 0.0,
            "duration": 1.25,
        },
        {"type": "transcript.done", "text": "hello there", "duration": 1.25},
    ):
        await ws.send(json.dumps(frame))


async def _xai_error_handler(ws: ServerConnection, recorded: dict[str, Any]) -> None:
    await ws.send(json.dumps({"type": "error", "message": "invalid api key"}))


def _events(client: Any, pcm_frames: list[bytes]) -> list[TranscriptEvent]:
    with client:
        for frame in pcm_frames:
            client.send_pcm(frame)
        client.finish()
        return list(client)


def test_libraxis_client_speaks_stt_ws_v1_and_yields_events() -> None:
    pcm = [b"\x01\x00" * 160, b"\x02\x00" * 160]
    with _FakeServer(_libraxis_handler) as server:
        client = LibraxisStreamClient(
            f"ws://127.0.0.1:{server.port}/v1/audio/transcribe",
            api_key="lx-" + "test",  # pragma: allowlist secret
            sample_rate=16000,
            language="pl",
            session_id="sess-1",
        )
        events = _events(client, pcm)

    assert server.recorded["headers"]["Authorization"] == "Bearer lx-test"
    start = server.recorded["text"][0]
    assert start["type"] == "session.start"
    assert start["protocol_version"] == 1
    assert start["session_id"] == "sess-1"
    assert start["locale"] == "pl"
    assert start["audio"] == {
        "encoding": "pcm_s16le",
        "sample_rate_hz": 16000,
        "channels": 1,
        "frame_header": "sequence_u64_be",
    }
    # Client-side stop = flush then end (what Codescribe sends on stop).
    assert [f["type"] for f in server.recorded["text"][1:]] == ["flush", "end"]
    binaries = server.recorded["binary"]
    assert [struct.unpack(">Q", b[:8])[0] for b in binaries] == [1, 2]
    assert [b[8:] for b in binaries] == pcm

    kinds = [(e.kind, e.text) for e in events]
    assert kinds == [
        ("partial", "dzień"),
        ("partial", "dzień dob"),
        ("final", "dzień dobry"),
        ("partial", "kolejne"),
        ("final", "kolejne zdanie"),
        ("ended", ""),
    ]
    first_final = events[2]
    assert (first_final.utterance_id, first_final.start_ms, first_final.end_ms) == (7, 120, 980)
    # Frames without an utterance_id keep a monotonic local counter.
    assert events[4].utterance_id == 8


def test_xai_client_query_string_frames_and_final_semantics() -> None:
    pcm = [b"\x00\x01" * 80]
    with _FakeServer(_xai_handler) as server:
        client = XaiStreamClient(
            api_key="xai-" + "test",  # pragma: allowlist secret
            sample_rate=16000,
            language="en",
            endpoint=f"ws://127.0.0.1:{server.port}/v1/stt",
        )
        events = _events(client, pcm)

    assert server.recorded["headers"]["Authorization"] == "Bearer xai-test"
    path = server.recorded["path"]
    assert path.startswith("/v1/stt?")
    query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
    assert query == {
        "encoding": "pcm",
        "sample_rate": "16000",
        "interim_results": "true",
        "language": "en",
    }
    # Raw PCM frames, no header; then Finalize + audio.done.
    assert server.recorded["binary"] == pcm
    assert server.recorded["text"] == [{"type": "Finalize"}, {"type": "audio.done"}]

    assert [(e.kind, e.text) for e in events] == [
        ("partial", "hello"),
        ("partial", "hello there"),
        ("final", "hello there"),
        ("ended", ""),
    ]
    final = events[2]
    assert (final.start_ms, final.end_ms, final.utterance_id) == (0, 1250, 1)


def test_xai_error_frame_ends_iteration_with_error_event() -> None:
    with _FakeServer(_xai_error_handler) as server:
        client = XaiStreamClient(
            api_key="k", sample_rate=16000, endpoint=f"ws://127.0.0.1:{server.port}/v1/stt"
        )
        with client:
            client.finish()
            events = list(client)
    assert events[-1].kind == "error"
    assert "invalid api key" in events[-1].text


def test_connection_refused_surfaces_as_error_event() -> None:
    client = LibraxisStreamClient("ws://127.0.0.1:9/nope", api_key="k", sample_rate=16000)
    with client:
        client.finish()
        events = list(client)
    assert [e.kind for e in events] == ["error"]


def test_send_after_finish_is_rejected() -> None:
    with _FakeServer(_libraxis_handler) as server:
        client = LibraxisStreamClient(
            f"ws://127.0.0.1:{server.port}/v1/audio/transcribe", api_key="k", sample_rate=16000
        )
        with client:
            client.finish()
            with pytest.raises(RuntimeError):
                client.send_pcm(b"\x00\x00")
            list(client)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("wss://api.x.ai/v1/stt", XaiStreamClient),
        ("wss://api.libraxis.cloud/v1/audio/transcribe", LibraxisStreamClient),
        ("ws://127.0.0.1:7237/live", LibraxisStreamClient),
    ],
)
def test_stream_client_for_endpoint_routes_by_host(endpoint: str, expected: type) -> None:
    client = stream_client_for_endpoint(endpoint, api_key="k", sample_rate=16000, language="pl")
    assert isinstance(client, expected)


def _iter_kinds(events: Iterator[TranscriptEvent]) -> list[str]:
    return [e.kind for e in events]
