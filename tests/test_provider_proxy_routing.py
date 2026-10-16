"""Real loopback servers prove plaintext never reaches an environment proxy."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from screenscribe.agent import chat
from screenscribe.frame_ocr import _post_ocr_request
from screenscribe.providers.xai import list_voices_xai, synthesize_speech_xai, transcribe_file_xai
from screenscribe.stt_stream import LibraxisStreamClient
from screenscribe.transcribe import _request_transcription_payload
from tests.test_stt_stream import _events, _FakeServer, _libraxis_handler

MOCK_KEY = "synthetic"  # pragma: allowlist secret -- local test only


def _sse_events(path: str) -> bytes:
    if path.endswith("/messages"):
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "test",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "synthetic reply"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    else:
        events = [
            {"type": "response.output_text.delta", "delta": "synthetic reply"},
            {
                "type": "response.completed",
                "response": {
                    "id": "response_test",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "synthetic reply"}],
                        }
                    ],
                },
            },
        ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[dict[str, Any]] = []
        self.connections = 0

    def get_request(self) -> Any:
        connection = super().get_request()
        self.connections += 1
        return connection


class _Handler(BaseHTTPRequestHandler):
    def _respond(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        server.requests.append(
            {
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization") or self.headers.get("X-Api-Key"),
            }
        )
        if self.path.endswith(("/messages", "/responses")):
            payload = _sse_events(self.path)
            content_type = "text/event-stream"
        else:
            payload = json.dumps(
                {
                    "text": "synthetic reply",
                    "language": "en",
                    "duration": 0.1,
                    "words": [],
                    "voices": [],
                }
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_CONNECT(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        server.requests.append({"path": self.path, "body": b"", "auth": None})
        self.send_error(502, "Synthetic proxy trap")

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _http_server() -> Iterator[_Server]:
    server = _Server()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def proxy_trap(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    for name in ["http_proxy", "https_proxy", "all_proxy", "no_proxy", "ws_proxy", "wss_proxy"]:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    with _http_server() as proxy:
        for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY"]:
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("NO_PROXY", "")
        yield proxy


@pytest.mark.parametrize("operation", ["tts", "voices", "xai-stt", "stt", "ocr"])
def test_local_provider_calls_go_direct_with_all_environment_proxies(
    proxy_trap: _Server, operation: str
) -> None:
    with _http_server() as target:
        endpoint = f"http://localhost:{target.server_port}/voice"
        if operation == "tts":
            synthesize_speech_xai(
                "synthetic audio text", api_key=MOCK_KEY, language="en", endpoint=endpoint
            )
        elif operation == "voices":
            list_voices_xai(api_key=MOCK_KEY, endpoint=endpoint)
        elif operation == "xai-stt":
            transcribe_file_xai(
                b"synthetic-audio", "test.wav", api_key=MOCK_KEY, language="en", endpoint=endpoint
            )
        elif operation == "stt":
            _request_transcription_payload(
                b"synthetic-audio", "test.wav", api_key=MOCK_KEY, stt_endpoint=endpoint
            )
        else:
            _post_ocr_request(endpoint, MOCK_KEY, {"image": "synthetic"})
        assert len(target.requests) == 1
        if operation != "stt":  # Local STT intentionally omits Authorization.
            assert target.requests[0]["auth"] == f"Bearer {MOCK_KEY}"
        if operation != "voices":
            assert b"synthetic" in target.requests[0]["body"]
        assert proxy_trap.requests == []
        assert proxy_trap.connections == 0


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_real_async_provider_clients_ignore_environment_proxy_for_loopback(
    proxy_trap: _Server, protocol: str
) -> None:
    if protocol == "anthropic":
        pytest.importorskip("anthropic")
    with _http_server() as target:
        path = "responses" if protocol == "responses" else "messages"
        endpoint = f"http://localhost:{target.server_port}/v1/{path}"
        provider = chat.AgentProvider(
            name="local",
            protocol=protocol,
            key=MOCK_KEY,
            model="test",
            url=endpoint,
            trust="local",
            slot="primary",
            host="localhost",
        )
        call = chat._responses_round if protocol == "responses" else chat._anthropic_round
        result = asyncio.run(
            call(provider, {"input": [{"role": "user", "content": "synthetic media"}]})
        )
        assert result.text == "synthetic reply"
        assert len(target.requests) == 1
        assert MOCK_KEY in target.requests[0]["auth"]
        assert b"synthetic media" in target.requests[0]["body"]
        assert proxy_trap.requests == []
        assert proxy_trap.connections == 0


def test_real_local_websocket_ignores_environment_proxy(proxy_trap: _Server) -> None:
    with _FakeServer(_libraxis_handler) as target:
        client = LibraxisStreamClient(f"ws://127.0.0.1:{target.port}/live", api_key=MOCK_KEY)
        events = _events(client, [b"\x01\x00" * 160])
        assert not any(event.kind == "error" for event in events)
        assert target.recorded["binary"][0].endswith(b"\x01\x00" * 160)
        assert target.recorded["headers"]["Authorization"] == f"Bearer {MOCK_KEY}"
        assert proxy_trap.requests == []
        assert proxy_trap.connections == 0


def test_https_keeps_environment_proxy_support(proxy_trap: _Server) -> None:
    # The proxy deliberately rejects CONNECT. Both possible destinations are
    # local; even a regression cannot contact an external host.
    with _http_server() as target:
        with pytest.raises(httpx.ProxyError):
            synthesize_speech_xai(
                "synthetic text",
                api_key=MOCK_KEY,
                language="en",
                endpoint=f"https://localhost:{target.server_port}/voice",
            )
        assert proxy_trap.connections == 1
        assert len(proxy_trap.requests) == 1
        assert proxy_trap.requests[0]["body"] == b""
        assert proxy_trap.requests[0]["auth"] is None
        assert target.connections == 0
