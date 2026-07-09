"""Transcription using LibraxisAI STT API."""

from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .api_utils import retry_request
from .config import LIBRAXIS_STT_ENDPOINT

# Value types and timeline helpers live in the leaf ``transcribe_types`` module.
# They are re-exported here (``X as X``) so the ~35 consumers importing them from
# ``screenscribe.transcribe`` keep working unchanged, and so this transport module
# still owns Segment/TranscriptionResult construction.
from .transcribe_types import (
    MIN_TIMELINE_GUARD_VIDEO_SECONDS as MIN_TIMELINE_GUARD_VIDEO_SECONDS,
)
from .transcribe_types import (
    MIN_TRANSCRIPT_TIMELINE_COVERAGE as MIN_TRANSCRIPT_TIMELINE_COVERAGE,
)
from .transcribe_types import (
    Segment as Segment,
)
from .transcribe_types import (
    TranscriptionResult as TranscriptionResult,
)
from .transcribe_types import (
    calculate_transcript_timeline_coverage as calculate_transcript_timeline_coverage,
)
from .transcribe_types import (
    transcript_last_segment_end as transcript_last_segment_end,
)
from .transcribe_types import (
    transcript_timeline_coverage_is_safe as transcript_timeline_coverage_is_safe,
)

console = Console()

# Default LibraxisAI STT endpoint (used if not configured otherwise)
LOCAL_STT_URL = "http://localhost:7237/transcribe"

# --- Anti-hallucination segment filter (FW-09) -----------------------------
# Whisper-family STT fabricates plausible outro captions ("thanks for watching")
# on music / silence, often with CONFIDENT metadata, so no single field is
# decisive. Thresholds below combine Whisper's own decode gates with two
# physically-grounded timing signals measured on real hallucinated output
# (Utah001.mp4: 46s of background music, zero speech -> two fabricated outro
# segments). Tune here; these are intentionally module-level constants, not env
# vars, so behaviour stays deterministic across runs.
#
# Confidence / repetition gates mirror openai-whisper's internal decoding
# thresholds (whisper.decoding / transcribe defaults: logprob_threshold=-1.0,
# no_speech_threshold=0.6, compression_ratio_threshold=2.4).
HALLUCINATION_NO_SPEECH_THRESHOLD = 0.6
HALLUCINATION_LOGPROB_THRESHOLD = -1.0
HALLUCINATION_COMPRESSION_RATIO_THRESHOLD = 2.4
# A real STT segment cannot end past the actual audio; timing beyond the file
# duration (+ rounding slack) is fabricated. Measured: Utah seg1 spanned
# 30.0-59.98s over a 46.37s file.
HALLUCINATION_PHANTOM_TIMING_SLACK_SECONDS = 1.0
# Whisper stretches a short hallucinated caption across an entire ~30s decode
# window on non-speech audio, yielding an implausibly low word rate. Real speech
# runs ~2-4 words/s; measured Utah segments ran 0.10-0.27 words/s. Bounded to a
# single decode window (~30s, upper bound 45s): a genuine brief utterance below
# the lower bound is never dropped, and a much longer span is timestamp
# compression/drift of REAL narration (handled by the coverage guard), not a
# per-window hallucination.
HALLUCINATION_MIN_LONG_SEGMENT_SECONDS = 15.0
HALLUCINATION_MAX_LONG_SEGMENT_SECONDS = 45.0
HALLUCINATION_MAX_WORDS_PER_SECOND = 0.5
MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _resolve_stt_url(use_local: bool, stt_endpoint: str | None) -> str:
    """Resolve the STT endpoint URL."""
    if use_local:
        return LOCAL_STT_URL
    if stt_endpoint:
        return stt_endpoint
    return LIBRAXIS_STT_ENDPOINT


def _normalize_content_type(filename: str, content_type: str | None = None) -> str:
    """Normalize content type for upload payloads."""
    if isinstance(content_type, str):
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized:
            return normalized
    return MIME_TYPES.get(Path(filename).suffix.lower(), "audio/mpeg")


def _coerce_segment_float(value: Any, fallback: float = 0.0) -> float:
    """Coerce an API-provided segment number to a float with a safe fallback.

    The STT API may return ``start``/``end`` as strings or ``None`` even though
    ``Segment.start``/``Segment.end`` are declared ``float``. Non-convertible or
    non-finite values fall back to ``fallback`` so downstream timeline math (and
    the HTML renderer's ``data-timestamp``) always receives a real number.
    """
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return fallback
    if coerced != coerced or coerced in (float("inf"), float("-inf")):
        return fallback
    return coerced


def _build_transcription_result(payload: dict[str, Any], language: str) -> TranscriptionResult:
    """Convert API payload into the shared TranscriptionResult structure."""
    segments: list[Segment] = []
    for seg in payload.get("segments", []):
        if not isinstance(seg, dict):
            continue
        segments.append(
            Segment(
                id=seg.get("id", 0),
                start=_coerce_segment_float(seg.get("start", 0.0)),
                end=_coerce_segment_float(seg.get("end", 0.0)),
                text=str(seg.get("text", "")).strip(),
                no_speech_prob=_coerce_segment_float(seg.get("no_speech_prob", 0.0)),
                avg_logprob=_coerce_segment_float(seg.get("avg_logprob", 0.0)),
                compression_ratio=_coerce_segment_float(seg.get("compression_ratio", 0.0)),
            )
        )

    full_text = str(payload.get("text", "")).strip()
    timestamps_are_synthetic = False
    if not segments and full_text:
        console.print("[yellow]API returned text without segments, creating synthetic segment[/]")
        word_count = len(full_text.split())
        estimated_duration = (word_count / 150) * 60
        segments.append(
            Segment(
                id=0,
                start=0.0,
                end=estimated_duration,
                text=full_text,
                no_speech_prob=0.0,
            )
        )
        # The end above is a speaking-rate estimate, not a real STT timestamp.
        timestamps_are_synthetic = True

    stt_response_id = str(payload.get("response_id", ""))
    if stt_response_id:
        console.print(f"[dim]  STT response_id for LLM chaining: {stt_response_id[:20]}...[/]")

    console.print(f"[green]Transcription complete:[/] {len(segments)} segments")

    detected_language = payload.get("language", language)
    if not isinstance(detected_language, str) or not detected_language.strip():
        detected_language = language

    return TranscriptionResult(
        text=full_text,
        segments=segments,
        language=detected_language,
        response_id=stt_response_id,
        timestamps_are_synthetic=timestamps_are_synthetic,
    )


def _request_transcription_payload(
    audio_data: bytes,
    filename: str,
    *,
    content_type: str | None = None,
    language: str = "en",
    use_local: bool = False,
    api_key: str | None = None,
    stt_endpoint: str | None = None,
    stt_model: str = "whisper-1",
    response_format: str = "json",
) -> dict[str, Any]:
    """Send audio bytes to STT and return the raw JSON payload."""
    if not audio_data:
        raise ValueError("Audio payload is empty")

    if not api_key and not use_local:
        raise ValueError(
            "API key required for cloud STT. Set SCREENSCRIBE_API_KEY, "
            "run `screenscribe config --set-key YOUR_KEY`, or use --local for local STT."
        )

    url = _resolve_stt_url(use_local, stt_endpoint)
    mime_type = _normalize_content_type(filename, content_type)
    is_local_endpoint = url.startswith("http://127.0.0.1") or url.startswith("http://localhost")
    field_name = "audio" if is_local_endpoint else "file"
    files = {field_name: (filename, audio_data, mime_type)}
    data = {
        "model": stt_model,
        "language": language,
        "response_format": response_format,
    }
    headers: dict[str, str] = {}
    if api_key and not use_local and not is_local_endpoint:
        headers["Authorization"] = f"Bearer {api_key}"

    def do_transcribe() -> httpx.Response:
        with httpx.Client(timeout=600.0) as client:
            response = client.post(url, files=files, data=data, headers=headers)
            response.raise_for_status()
            return response

    response = retry_request(
        do_transcribe,
        max_retries=3,
        operation_name="STT transcription",
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("STT API returned unexpected payload shape")
    return payload


def transcribe_audio_bytes(
    audio_data: bytes,
    filename: str,
    *,
    content_type: str | None = None,
    language: str = "en",
    use_local: bool = False,
    api_key: str | None = None,
    stt_endpoint: str | None = None,
    stt_model: str = "whisper-1",
    response_format: str = "json",
) -> TranscriptionResult:
    """Transcribe in-memory audio payloads, suitable for browser uploads."""
    payload = _request_transcription_payload(
        audio_data,
        filename,
        content_type=content_type,
        language=language,
        use_local=use_local,
        api_key=api_key,
        stt_endpoint=stt_endpoint,
        stt_model=stt_model,
        response_format=response_format,
    )

    return _build_transcription_result(payload, language)


def transcribe_audio(
    audio_path: Path,
    language: str = "en",
    use_local: bool = False,
    api_key: str | None = None,
    stt_endpoint: str | None = None,
    stt_model: str = "whisper-1",
) -> TranscriptionResult:
    """
    Transcribe audio using LibraxisAI STT.

    Args:
        audio_path: Path to audio file
        language: Language code (default: pl)
        use_local: Use local STT server instead of cloud
        api_key: LibraxisAI API key
        stt_endpoint: Custom STT endpoint URL (overrides default)

    Returns:
        TranscriptionResult with full text and segments
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Validate API key for cloud usage
    if not api_key and not use_local:
        raise ValueError(
            "API key required for cloud STT. Set SCREENSCRIBE_API_KEY, "
            "run `screenscribe config --set-key YOUR_KEY`, or use --local for local STT."
        )

    console.print(f"[blue]Transcribing:[/] {audio_path.name}")
    console.print(f"[dim]Using {'local' if use_local else 'cloud'} STT[/]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Transcribing audio...", total=None)
        with open(audio_path, "rb") as handle:
            audio_data = handle.read()

        payload = _request_transcription_payload(
            audio_data,
            audio_path.name,
            content_type=_normalize_content_type(audio_path.name),
            language=language,
            use_local=use_local,
            api_key=api_key,
            stt_endpoint=stt_endpoint,
            stt_model=stt_model,
            response_format="verbose_json",
        )
    return _build_transcription_result(payload, language)


def transcribe_audio_chunked(
    audio_path: Path,
    language: str = "en",
    use_local: bool = False,
    api_key: str | None = None,
    stt_endpoint: str | None = None,
    stt_model: str = "whisper-1",
    chunk_duration: float = 60.0,
    overlap: float = 5.0,
) -> TranscriptionResult:
    """Transcribe long audio by splitting it into silence-aware chunks.

    Whisper-style STT drifts timestamps progressively on recordings longer than
    a few minutes (seek accumulation, hallucination-skip on silence, encoder
    delay). This splits the audio at natural pauses, transcribes each chunk on
    its own, offsets the per-chunk timestamps back to the global timeline, and
    drops segments duplicated across the overlap zone. Short audio takes the
    ordinary single-shot path unchanged.

    Per-chunk transcription is delegated to the module-level ``transcribe_audio``
    (resolved at call time, so ``monkeypatch.setattr("screenscribe.transcribe.
    transcribe_audio", ...)`` intercepts it), so the STT endpoint/key resolution
    stays driven by the caller's config.

    Args:
        audio_path: Path to audio file.
        language: Language code.
        use_local: Use the local STT server instead of cloud.
        api_key: STT API key.
        stt_endpoint: Custom STT endpoint URL (overrides default).
        stt_model: STT model name.
        chunk_duration: Target chunk size in seconds (default: 60).
        overlap: Overlap between chunks in seconds (default: 5).

    Returns:
        TranscriptionResult with timestamp-accurate, merged segments.
    """
    from . import audio

    try:
        duration = audio.get_audio_duration(audio_path)
    except (OSError, RuntimeError, ValueError):
        # Undecodable or unprobeable input (e.g. a stub file in tests, or a
        # missing ffprobe): fall back to a single-shot transcription rather
        # than failing the whole pipeline on a duration probe.
        duration = None

    # Short audio (or unknown duration) does not benefit from chunking.
    if duration is None or duration <= chunk_duration:
        return transcribe_audio(
            audio_path,
            language=language,
            use_local=use_local,
            api_key=api_key,
            stt_endpoint=stt_endpoint,
            stt_model=stt_model,
        )

    console.print(
        f"[blue]Long audio ({duration:.0f}s) — using silence-aware chunked transcription[/]"
    )
    try:
        chunks = audio.split_audio_chunks(
            audio_path, max_chunk_duration=chunk_duration, overlap=overlap
        )
    except RuntimeError as exc:
        # A chunk split failed mid-recording. Rather than transcribe a partial
        # prefix as if it were complete, fall back to a single-shot pass over the
        # whole file (same fallback as the short-audio / unknown-duration path).
        console.print(
            f"[yellow]Chunked split failed ({exc}); falling back to single-shot transcription[/]"
        )
        return transcribe_audio(
            audio_path,
            language=language,
            use_local=use_local,
            api_key=api_key,
            stt_endpoint=stt_endpoint,
            stt_model=stt_model,
        )

    all_segments: list[Segment] = []
    last_response_id = ""
    any_synthetic = False

    try:
        for index, (chunk_path, offset) in enumerate(chunks):
            console.print(f"[dim]  Chunk {index + 1}/{len(chunks)} (offset {offset:.0f}s)...[/]")

            result = transcribe_audio(
                chunk_path,
                language=language,
                use_local=use_local,
                api_key=api_key,
                stt_endpoint=stt_endpoint,
                stt_model=stt_model,
            )

            if result.response_id:
                last_response_id = result.response_id

            # A chunk whose STT backend returned text without segment timings has
            # speaking-rate-estimated timestamps. The merged timeline then mixes
            # real and estimated timing, so it must NOT feed the coverage guard or
            # screenshot selection as if it were real STT timing -- carry the flag
            # forward when any chunk is synthetic (finding G).
            if result.timestamps_are_synthetic:
                any_synthetic = True

            for seg in result.segments:
                adjusted_start = seg.start + offset
                adjusted_end = seg.end + offset

                # Drop segments in the overlap zone that duplicate the previous
                # chunk's tail (within a 1s slack window). Skip this for synthetic
                # chunks: their timings are speaking-rate estimates spanning the
                # whole chunk, so the overlapped next chunk's estimated start lands
                # inside the previous tail and the heuristic would drop the entire
                # chunk as a false duplicate, losing its text (M3, edge of G).
                #
                # Only a FULLY-covered overlap segment is a duplicate (finding O3).
                # A segment whose start sits in the overlap but whose end extends
                # past the prior tail carries unique speech after the seam -- a
                # common STT boundary shape -- so testing the start alone would
                # drop that post-seam content from both `segments` and the merged
                # text. Require the adjusted end to stay within the prior tail
                # (same 1s slack) before treating it as a duplicate.
                if (
                    not result.timestamps_are_synthetic
                    and all_segments
                    and adjusted_start < all_segments[-1].end - 1.0
                    and adjusted_end <= all_segments[-1].end + 1.0
                ):
                    continue

                all_segments.append(
                    Segment(
                        id=len(all_segments),
                        start=adjusted_start,
                        end=adjusted_end,
                        text=seg.text,
                        no_speech_prob=seg.no_speech_prob,
                        avg_logprob=seg.avg_logprob,
                        compression_ratio=seg.compression_ratio,
                    )
                )
    finally:
        # Remove the temporary chunk WAVs produced by split_audio_chunks, even on
        # error, so they do not accumulate in the temp dir. Never delete the
        # caller's original audio (returned as its own "chunk" only on the
        # short-audio path, which does not reach here).
        chunk_dirs: set[Path] = set()
        for chunk_path, _offset in chunks:
            if chunk_path != audio_path:
                chunk_dirs.add(chunk_path.parent)
                try:
                    chunk_path.unlink(missing_ok=True)
                except OSError:
                    pass
        # Also drop the now-empty per-run chunk directory (mkdtemp creates one per
        # split). Best-effort: ignore if not empty or already gone.
        for chunk_dir in chunk_dirs:
            try:
                chunk_dir.rmdir()
            except OSError:
                pass

    console.print(
        f"[green]Chunked transcription complete:[/] "
        f"{len(all_segments)} segments from {len(chunks)} chunks"
    )

    # Build the merged transcript text from the deduplicated segments rather than
    # the raw per-chunk `result.text`: the overlap zone is dropped from the
    # segment timeline, but concatenating per-chunk text would repeat the seam
    # phrase at every chunk boundary.
    merged_text = " ".join(seg.text.strip() for seg in all_segments if seg.text.strip())

    return TranscriptionResult(
        text=merged_text,
        segments=all_segments,
        language=language,
        response_id=last_response_id,
        timestamps_are_synthetic=any_synthetic,
    )


def _segment_hallucination_reason(segment: Segment, audio_duration: float | None) -> str | None:
    """Return a human-readable reason if the segment looks hallucinated, else None."""
    # 1. Phantom timing: real STT cannot timestamp content past the audio end.
    if audio_duration is not None and audio_duration > 0:
        slack = HALLUCINATION_PHANTOM_TIMING_SLACK_SECONDS
        if segment.start >= audio_duration + slack:
            return f"starts past audio end ({segment.start:.1f}s ≥ {audio_duration:.1f}s)"
        if segment.end > audio_duration + slack:
            return f"ends past audio end ({segment.end:.1f}s > {audio_duration:.1f}s)"

    # 2. Whisper decode-confidence gate (canonical): low confidence AND flagged
    #    as no-speech. Both must hold to avoid dropping confident real speech.
    if (
        segment.no_speech_prob > HALLUCINATION_NO_SPEECH_THRESHOLD
        and segment.avg_logprob < HALLUCINATION_LOGPROB_THRESHOLD
    ):
        return (
            f"no_speech_prob={segment.no_speech_prob:.2f} > "
            f"{HALLUCINATION_NO_SPEECH_THRESHOLD} & "
            f"avg_logprob={segment.avg_logprob:.2f} < {HALLUCINATION_LOGPROB_THRESHOLD}"
        )

    # 3. Repetition gate (canonical): a highly compressible caption is repeated
    #    tokens (e.g. "you you you ...") -- Whisper's classic silence loop.
    if segment.compression_ratio > HALLUCINATION_COMPRESSION_RATIO_THRESHOLD:
        return (
            f"compression_ratio={segment.compression_ratio:.2f} > "
            f"{HALLUCINATION_COMPRESSION_RATIO_THRESHOLD}"
        )

    # 4. Stretched caption: a short phrase spread across a long decode window is
    #    Whisper filling a non-speech window with one plausible sentence.
    span = segment.end - segment.start
    if HALLUCINATION_MIN_LONG_SEGMENT_SECONDS <= span <= HALLUCINATION_MAX_LONG_SEGMENT_SECONDS:
        words = len(segment.text.split())
        rate = words / span if span > 0 else 0.0
        if rate < HALLUCINATION_MAX_WORDS_PER_SECOND:
            return (
                f"{words} words over {span:.0f}s = {rate:.2f} words/s "
                f"< {HALLUCINATION_MAX_WORDS_PER_SECOND} (non-speech window)"
            )

    return None


def filter_hallucinated_segments(
    result: TranscriptionResult,
    audio_duration: float | None = None,
    *,
    verbose: bool = False,
) -> TranscriptionResult:
    """Drop STT segments that look like hallucinations on non-speech audio.

    Whisper-family models invent plausible captions (outros, "thank you for
    watching") on music or silence -- sometimes with confident metadata -- which
    then pollute the transcript, timeline and report. This drops such segments
    using measured signals (see the ``HALLUCINATION_*`` thresholds) and rebuilds
    the transcript text from the survivors. Every drop is logged (never a silent
    disappearance); ``verbose`` adds the per-segment reason.

    Synthetic-timestamp results (API returned text without segment timings) carry
    no real decode metadata and their estimated end can exceed the real audio, so
    they are returned untouched.

    Returns a new ``TranscriptionResult``; the input is not mutated.
    """
    if not result.segments or result.timestamps_are_synthetic:
        return result

    kept: list[Segment] = []
    dropped: list[tuple[Segment, str]] = []
    for segment in result.segments:
        reason = _segment_hallucination_reason(segment, audio_duration)
        if reason is None:
            kept.append(segment)
        else:
            dropped.append((segment, reason))

    if not dropped:
        return result

    console.print(
        f"[yellow]Filtered {len(dropped)} likely-hallucinated segment(s) "
        f"(no-speech / music / silence)[/]"
    )
    if verbose:
        for segment, reason in dropped:
            preview = segment.text[:60] + ("…" if len(segment.text) > 60 else "")
            console.print(
                f"[dim]  drop [{segment.start:.1f}-{segment.end:.1f}s] {preview!r}: {reason}[/]"
            )

    merged_text = " ".join(seg.text.strip() for seg in kept if seg.text.strip())
    return TranscriptionResult(
        text=merged_text,
        segments=kept,
        language=result.language,
        response_id=result.response_id,
        timestamps_are_synthetic=result.timestamps_are_synthetic,
        hallucinations_filtered=True,
    )


def validate_audio_quality(result: TranscriptionResult) -> tuple[bool, str | None, bool]:
    """
    Validate that audio actually contains speech.

    Detects silent/near-silent recordings by analyzing no_speech_prob
    from Whisper and checking for repetitive hallucinations.

    Args:
        result: TranscriptionResult from transcribe_audio()

    Returns:
        Tuple of (is_valid, message, is_warning).
        If is_valid is False, message contains user-friendly feedback.
        If is_warning is True, pipeline should continue after showing the warning.
    """
    if not result.segments:
        if result.hallucinations_filtered:
            # The recording decoded fine but every segment was a no-speech
            # hallucination (e.g. background music over landscape footage). That
            # is a legitimate "no speech" outcome, not a corrupt file -- let the
            # pipeline continue to a clean, empty no-findings report.
            return (
                True,
                "⚠️  No speech detected in this recording.\n"
                "   The audio contained only music/ambient sound, so the STT "
                "hallucinations were removed. No transcript findings will be produced.",
                True,
            )
        return (
            False,
            "⚠️  No audio segments detected!\n   The audio file appears to be empty or corrupted.",
            False,
        )

    # Calculate average no_speech probability
    avg_no_speech = sum(s.no_speech_prob for s in result.segments) / len(result.segments)

    transcript_text = result.text.strip()
    if not transcript_text:
        transcript_text = " ".join(s.text for s in result.segments if s.text)
    # BH24/BH36: count words on the same punctuation-normalized basis we later
    # compare against the generic-hallucination phrase set. Counting raw tokens
    # let stray punctuation (". , ! thank you") inflate the count past the
    # word_count<=8 gate and silently skip the hallucination guard.
    normalized_transcript = " ".join(
        transcript_text.lower().replace(".", " ").replace(",", " ").replace("!", " ").split()
    )
    word_count = len(normalized_transcript.split())

    suppress_warning_words = 150
    stop_words_threshold = 40
    stop_no_speech_threshold = 0.85
    warn_no_speech_threshold = 0.75

    generic_silence_hallucinations = {
        "thank you for watching",
        "thanks for watching",
        "thank you",
    }
    if word_count <= 8 and normalized_transcript in generic_silence_hallucinations:
        return (
            False,
            "⚠️  Detected likely silent audio hallucination!\n"
            f"   STT returned the generic phrase '{transcript_text}'.\n"
            "   This often happens with very short, quiet, or empty recordings.\n"
            "\n"
            "   Please record a longer note or check microphone input.",
            False,
        )

    # Only stop on very high no_speech + very short transcript.
    if avg_no_speech > stop_no_speech_threshold and word_count < stop_words_threshold:
        return (
            False,
            f"⚠️  Audio appears to contain little or no speech!\n"
            f"   Average no-speech probability: {avg_no_speech:.0%}\n"
            f"\n"
            f"   Common causes:\n"
            f"   • Microphone was not enabled during screen recording\n"
            f"   • Microphone input volume is too low (check System Settings > Sound > Input)\n"
            f"   • Wrong audio input device selected\n"
            f"\n"
            f"   Tip: When using Cmd+Shift+5, click 'Options' and select your microphone.",
            False,
        )

    # Suppress warning if transcript is long enough to be meaningful.
    if word_count >= suppress_warning_words:
        return True, None, False

    if avg_no_speech >= warn_no_speech_threshold:
        return (
            True,
            f"⚠️  High no-speech score detected, but transcript has content.\n"
            f"   Average no-speech probability: {avg_no_speech:.0%}\n"
            f"   Word count: {word_count}\n"
            f"\n"
            f"   Continuing anyway. If results look wrong, check mic settings.",
            True,
        )

    # Check for repetitive hallucinations (Whisper hallucinates on silence)
    texts = [s.text.strip().lower() for s in result.segments if s.text.strip()]
    if texts:
        unique_ratio = len(set(texts)) / len(texts)
        if unique_ratio < 0.3 and len(texts) > 3:
            # More than 70% duplicates with multiple segments = likely hallucination
            most_common = max(set(texts), key=texts.count)
            return (
                False,
                f"⚠️  Detected repetitive transcription (likely silent audio)!\n"
                f"   The same phrase '{most_common}' appears repeatedly.\n"
                f"   This typically happens when Whisper hallucinates on silent input.\n"
                f"\n"
                f"   Please check your microphone settings and re-record.",
                False,
            )

    return True, None, False
