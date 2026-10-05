"""Live (streaming) speech-to-text over websockets.

One synchronous interface, :class:`StreamingSttClient`, backed by the
``websockets`` asyncio client running on a private thread. Callers push PCM16LE
mono frames with :meth:`send_pcm`, call :meth:`finish` once the audio is over,
and iterate :class:`TranscriptEvent` items until the terminal ``ended`` /
``error`` event.

Two gateways are wired:

``LibraxisStreamClient`` -- LibraxisAI ``stt-ws-v1`` (contract taken from
Codescribe ``core/asr_session/cloud.rs``, read 2026-09-10):

- first text frame ``{"type":"session.start","protocol_version":1,"session_id",
  "locale","vocabulary","audio":{"encoding":"pcm_s16le","sample_rate_hz",
  "channels":1,"frame_header":"sequence_u64_be"}}``;
- binary frames = 8-byte big-endian sequence id + PCM16LE bytes;
- client stop = ``{"type":"flush"}`` then ``{"type":"end"}`` (this is exactly
  what Codescribe sends on ``GatewayCommand::End``);
- server frames: ``hello|ack|ready|vad.sample|speech.start|speech.end`` are
  ignored, ``transcript.partial|transcript`` -> partial, ``transcript.final`` ->
  final, ``error{code}`` -> error, ``end|session.ended|stream.closed`` -> ended.

``XaiStreamClient`` -- xAI ``wss://api.x.ai/v1/stt`` (docs.x.ai, 2026-09-10):

- query ``encoding=pcm&sample_rate=<hz>&interim_results=true[&language=<code>]``;
- binary frames = raw PCM16LE;
- client stop = ``{"type":"Finalize"}`` then ``{"type":"audio.done"}``
  (docs spell the finalize frame ``Finalize``; Codescribe sends only ``audio.done``);
- server events: ``transcript.created`` ignored, ``transcript.partial{text,
  is_final,speech_final,start,duration}`` -> final only when ``is_final`` AND
  ``speech_final``, ``transcript.done`` -> ended, ``error{message}`` -> error.
"""

from __future__ import annotations

import asyncio
import json
import queue
import struct
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

EventKind = Literal["partial", "final", "ended", "error"]

LIBRAXIS_STT_LIVE_ENDPOINT = "wss://api.libraxis.cloud/v1/audio/transcribe"
XAI_STT_LIVE_ENDPOINT = "wss://api.x.ai/v1/stt"

# Time allowed for the server to flush its tail after the client end frames.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class TranscriptEvent:
    """One live transcript update. ``ended``/``error`` terminate iteration."""

    kind: EventKind
    text: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    utterance_id: int = 0
    code: str = ""


_END = object()


class StreamingSttClient:
    """Sync facade over an asyncio websocket session running on a worker thread.

    Subclasses provide the wire vocabulary via ``_start_frames``,
    ``_encode_pcm``, ``_end_frames`` and ``_adapt``.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url
        self._headers = dict(headers or {})
        self._connect_timeout = connect_timeout
        self._drain_timeout = drain_timeout
        self._events: queue.Queue[TranscriptEvent] = queue.Queue()
        self._outbox: asyncio.Queue[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = False
        self._terminal_emitted = False
        self._utterance_id = 0

    # -- wire hooks -------------------------------------------------------------
    def _start_frames(self) -> list[str]:
        return []

    def _encode_pcm(self, pcm: bytes) -> bytes:
        return pcm

    def _end_frames(self) -> list[str]:
        return []

    def _adapt(self, text: str) -> TranscriptEvent | None:
        raise NotImplementedError

    # -- sync surface -----------------------------------------------------------
    def __enter__(self) -> StreamingSttClient:
        self._thread = threading.Thread(
            target=self._thread_main, name="screenscribe-live-stt", daemon=True
        )
        self._thread.start()
        # Wait for the socket to open (or the worker to fail); failure is
        # reported as an ``error`` event so callers see one consistent surface.
        self._ready.wait(self._connect_timeout + 1.0)
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._finished:
            self.finish()
        if self._thread is not None:
            self._thread.join(self._drain_timeout + 5.0)

    def send_pcm(self, pcm: bytes) -> None:
        """Queue one PCM16LE mono frame for the gateway."""
        if self._finished:
            raise RuntimeError("send_pcm() after finish()")
        if not pcm:
            return
        self._post(self._encode_pcm(pcm))

    def finish(self) -> None:
        """Signal end of audio; the server tail is drained into the event queue."""
        if self._finished:
            return
        self._finished = True
        self._post(_END)

    def __iter__(self) -> Iterator[TranscriptEvent]:
        while True:
            event = self._events.get()
            yield event
            if event.kind in ("ended", "error"):
                return

    # -- internals -----------------------------------------------------------------
    def _post(self, item: Any) -> None:
        loop, outbox = self._loop, self._outbox
        if loop is None or outbox is None or loop.is_closed():
            # Worker never connected (or already died): nothing to send; the
            # terminal error event is already queued by the worker.
            return
        loop.call_soon_threadsafe(outbox.put_nowait, item)

    def _emit(self, event: TranscriptEvent) -> None:
        if event.kind in ("ended", "error"):
            if self._terminal_emitted:
                return
            self._terminal_emitted = True
        self._events.put(event)

    def _next_utterance_after_final(self) -> None:
        self._utterance_id += 1

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # worker must never die silently
            self._emit(TranscriptEvent(kind="error", text=str(exc), code="transport"))
        finally:
            self._ready.set()
            self._emit(TranscriptEvent(kind="ended"))

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._outbox = asyncio.Queue()
        try:
            async with connect(
                self.url,
                additional_headers=self._headers,
                open_timeout=self._connect_timeout,
                max_size=None,
            ) as ws:
                for frame in self._start_frames():
                    await ws.send(frame)
                self._ready.set()
                receiver = asyncio.create_task(self._receive(ws))
                await self._send_loop(ws)
                try:
                    await asyncio.wait_for(receiver, self._drain_timeout)
                except TimeoutError:
                    receiver.cancel()
                    self._emit(TranscriptEvent(kind="ended"))
        except ConnectionClosed as exc:
            self._emit(TranscriptEvent(kind="error", text=str(exc), code="closed"))
        except OSError as exc:
            self._emit(TranscriptEvent(kind="error", text=str(exc), code="transport"))
        except Exception as exc:  # handshake / protocol failures from websockets
            self._emit(TranscriptEvent(kind="error", text=str(exc), code="transport"))

    async def _send_loop(self, ws: Any) -> None:
        assert self._outbox is not None
        while True:
            item = await self._outbox.get()
            if item is _END:
                for frame in self._end_frames():
                    await ws.send(frame)
                return
            await ws.send(item)

    async def _receive(self, ws: Any) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                continue
            event = self._adapt(message)
            if event is None:
                continue
            self._emit(event)
            if event.kind in ("ended", "error"):
                return
        # Server closed without an explicit end frame.
        self._emit(TranscriptEvent(kind="ended"))


def _ms(seconds: Any) -> int | None:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value in (float("inf"),):
        return None
    return round(value * 1000)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class LibraxisStreamClient(StreamingSttClient):
    """LibraxisAI ``stt-ws-v1`` live transcription client."""

    def __init__(
        self,
        endpoint: str = LIBRAXIS_STT_LIVE_ENDPOINT,
        *,
        api_key: str,
        sample_rate: int = 16000,
        language: str = "en",
        vocabulary: str = "",
        session_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        if not api_key:
            raise ValueError("API key required for live STT")
        super().__init__(endpoint, headers={"Authorization": f"Bearer {api_key}"}, **kwargs)
        self.sample_rate = sample_rate
        self.language = language
        self.vocabulary = vocabulary
        self.session_id = session_id or uuid.uuid4().hex
        self._sequence = 0

    def _start_frames(self) -> list[str]:
        return [
            json.dumps(
                {
                    "type": "session.start",
                    "protocol_version": 1,
                    "session_id": self.session_id,
                    "locale": self.language,
                    "vocabulary": self.vocabulary,
                    "audio": {
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": self.sample_rate,
                        "channels": 1,
                        "frame_header": "sequence_u64_be",
                    },
                }
            )
        ]

    def _encode_pcm(self, pcm: bytes) -> bytes:
        self._sequence += 1
        return struct.pack(">Q", self._sequence) + pcm

    def _end_frames(self) -> list[str]:
        return [json.dumps({"type": "flush"}), json.dumps({"type": "end"})]

    def _adapt(self, text: str) -> TranscriptEvent | None:
        try:
            value = json.loads(text)
        except ValueError:
            return TranscriptEvent(kind="error", text="non-JSON frame", code="protocol")
        if not isinstance(value, dict):
            return None
        frame_type = value.get("type")
        if frame_type in ("hello", "ack", "ready", "vad.sample", "speech.start", "speech.end"):
            return None
        if frame_type in ("transcript.partial", "transcript", "transcript.final"):
            body = str(value.get("text", "")).strip()
            if not body:
                return None
            utterance = _int_or_none(value.get("utterance_id"))
            if utterance is not None:
                self._utterance_id = utterance
            event = TranscriptEvent(
                kind="final" if frame_type == "transcript.final" else "partial",
                text=body,
                start_ms=_int_or_none(value.get("start_ms")),
                end_ms=_int_or_none(value.get("end_ms")),
                utterance_id=self._utterance_id,
            )
            if frame_type == "transcript.final":
                self._next_utterance_after_final()
            return event
        if frame_type in ("error", "session.error"):
            code = str(value.get("code", "protocol"))
            return TranscriptEvent(kind="error", text=str(value.get("message", code)), code=code)
        if frame_type in ("end", "session.ended", "stream.closed"):
            return TranscriptEvent(kind="ended")
        return None


class XaiStreamClient(StreamingSttClient):
    """xAI ``wss://api.x.ai/v1/stt`` live transcription client."""

    def __init__(
        self,
        *,
        api_key: str,
        sample_rate: int = 16000,
        language: str | None = None,
        endpoint: str = XAI_STT_LIVE_ENDPOINT,
        **kwargs: Any,
    ) -> None:
        if not api_key:
            raise ValueError("API key required for live STT")
        params: dict[str, str] = {
            "encoding": "pcm",
            "sample_rate": str(sample_rate),
            "interim_results": "true",
        }
        if language:
            params["language"] = language
        joiner = "&" if "?" in endpoint else "?"
        super().__init__(
            f"{endpoint}{joiner}{urlencode(params)}",
            headers={"Authorization": f"Bearer {api_key}"},
            **kwargs,
        )
        self.sample_rate = sample_rate
        self.language = language
        self._utterance_id = 1

    def _end_frames(self) -> list[str]:
        return [json.dumps({"type": "Finalize"}), json.dumps({"type": "audio.done"})]

    def _adapt(self, text: str) -> TranscriptEvent | None:
        try:
            value = json.loads(text)
        except ValueError:
            return TranscriptEvent(kind="error", text="non-JSON frame", code="protocol")
        if not isinstance(value, dict):
            return None
        frame_type = value.get("type")
        if frame_type == "transcript.created":
            return None
        if frame_type == "transcript.done":
            return TranscriptEvent(kind="ended")
        if frame_type == "error":
            return TranscriptEvent(kind="error", text=str(value.get("message", "")), code="error")
        if frame_type == "transcript.partial":
            body = str(value.get("text", "")).strip()
            is_final = bool(value.get("is_final")) and bool(value.get("speech_final"))
            start_ms = _ms(value.get("start"))
            duration_ms = _ms(value.get("duration"))
            end_ms = None
            if start_ms is not None and duration_ms is not None:
                end_ms = start_ms + duration_ms
            event = TranscriptEvent(
                kind="final" if is_final else "partial",
                text=body,
                start_ms=start_ms,
                end_ms=end_ms,
                utterance_id=self._utterance_id,
            )
            if is_final:
                self._next_utterance_after_final()
            return event
        return None


def stream_client_for_endpoint(
    endpoint: str,
    *,
    api_key: str,
    sample_rate: int = 16000,
    language: str | None = None,
    **kwargs: Any,
) -> StreamingSttClient:
    """Pick the client by websocket host: ``api.x.ai`` -> xAI, anything else -> LibraxisAI wire."""
    host = (urlsplit(endpoint).hostname or "").lower()
    if host == "api.x.ai" or host.endswith(".x.ai"):
        return XaiStreamClient(
            api_key=api_key, sample_rate=sample_rate, language=language, endpoint=endpoint, **kwargs
        )
    return LibraxisStreamClient(
        endpoint, api_key=api_key, sample_rate=sample_rate, language=language or "en", **kwargs
    )
