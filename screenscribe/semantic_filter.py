"""Semantic filtering pipeline for transcript analysis.

This module provides semantic pre-filtering capabilities that analyze
the entire transcript using LLM before frame extraction, allowing
the vision model to analyze more potential findings.
"""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from .api_utils import build_llm_request_body, retry_request
from .config import ScreenScribeConfig
from .keywords import KeywordsConfig, format_keywords_hint
from .prompts import apply_analysis_prompt_override
from .text_similarity import _text_similarity
from .transcribe import Segment, TranscriptionResult

console = Console()


POI_CATEGORIES = ("bug", "change", "ui", "performance", "accessibility", "other")
PoiCategory = Literal["bug", "change", "ui", "performance", "accessibility", "other"]


def _validate_poi_category(raw: str) -> PoiCategory:
    """Narrow a raw string to a valid PointOfInterest category literal."""
    if raw in POI_CATEGORIES:
        return cast(PoiCategory, raw)
    return "other"


def _coerce_confidence(raw: Any, default: float = 0.5) -> float:
    """Coerce a model-supplied confidence value to a float.

    The LLM JSON is untrusted: ``confidence`` can arrive as a number, a numeric
    string (``"0.85"``), or a non-numeric string (``"high"``). ``PointOfInterest``
    is typed ``float`` and ``deduplicate_pois`` ranks/merges with ``max(...)``;
    a stray string makes that comparison raise ``TypeError``. Coerce here so the
    type invariant holds at construction time.
    """
    if isinstance(raw, bool):
        # bool is an int subclass; treat as default rather than 1.0/0.0.
        return default
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return default
    else:
        return default
    # json.loads parses NaN/Infinity/-Infinity by default and float("inf"/"nan")
    # succeeds, so a finite-looking float() can still be non-finite. NaN breaks
    # the max(...) ranking in deduplicate_pois (every comparison is False) and
    # +Inf permanently wins, evicting real findings. Reject non-finite here.
    if not math.isfinite(value):
        return default
    return value


def _coerce_timestamp(raw: Any, default: float = 0.0) -> float:
    """Coerce a model-supplied timestamp value to a float (same hazard as confidence)."""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return default
    else:
        return default
    # Reject non-finite (NaN/Inf): an Inf timestamp also poisons midpoint
    # (start+end)/2 and frame extraction. See _coerce_confidence.
    if not math.isfinite(value):
        return default
    return value


@dataclass
class PointOfInterest:
    """A point of interest identified by semantic pre-filtering.

    Represents a moment in the video that may contain a finding,
    identified by analyzing the transcript semantically before
    any frame extraction occurs.
    """

    timestamp_start: float
    timestamp_end: float
    category: Literal["bug", "change", "ui", "performance", "accessibility", "other"]
    confidence: float  # 0.0 - 1.0
    reasoning: str  # Why this was flagged
    transcript_excerpt: str  # The relevant text
    segment_ids: list[int] = field(default_factory=list)  # Source segment IDs

    @property
    def midpoint(self) -> float:
        """Get the midpoint timestamp for screenshot extraction."""
        return (self.timestamp_start + self.timestamp_end) / 2


@dataclass
class SemanticFilterResult:
    """Result of semantic pre-filtering with response_id for conversation chaining.

    The response_id enables context chaining to VLM analysis - the vision model
    will understand thematic context from the transcript analysis (e.g., knowing
    the user discussed "UI bugs" helps VLM better interpret screenshots).

    ``failed`` distinguishes "the LLM call failed" (auth/network/stream error or
    no key) from "the LLM ran and found nothing". The two look identical
    (``pois == []``) but mean opposite things: an empty result on a healthy key
    is a real deliverable, while a failed call must NOT be dressed up as a
    confident "no issues detected" report. ``error`` carries a short reason for
    user-facing messaging.
    """

    pois: list[PointOfInterest]
    response_id: str = ""  # API response ID for conversation chaining to VLM
    failed: bool = False  # True when the LLM call could not complete
    error: str = ""  # Short failure reason (empty on success)


# Prompts for semantic pre-filtering
SEMANTIC_PREFILTER_PROMPTS: dict[str, str] = {
    "pl": """Jesteś ekspertem UX/UI analizującym transkrypcję nagrania przeglądu aplikacji.

Przeanalizuj CAŁĄ poniższą transkrypcję i zidentyfikuj WSZYSTKIE momenty, w których użytkownik:
- Opisuje błąd, problem lub coś co nie działa
- Sugeruje zmianę lub ulepszenie
- Komentuje elementy UI/UX (przyciski, formularze, layout)
- Wspomina o problemach z wydajnością
- Porusza kwestie dostępności
- Opisuje cokolwiek co może wymagać uwagi developera

WAŻNE: Bądź LIBERALNY w identyfikacji - lepiej oznaczyć więcej momentów niż przegapić potencjalne problemy.
Model wizyjny później zweryfikuje każdy z nich analizując screenshot.
{keywords_hint}
Transkrypcja z timestampami:
{transcript_with_timestamps}

Odpowiedz w formacie JSON:
{{
    "points_of_interest": [
        {{
            "timestamp_start": 12.5,
            "timestamp_end": 18.0,
            "category": "bug|change|ui|performance|accessibility|other",
            "confidence": 0.85,
            "reasoning": "Użytkownik mówi że przycisk nie reaguje na kliknięcie",
            "transcript_excerpt": "ten przycisk tutaj jakoś nie działa"
        }}
    ],
    "total_issues_found": 5,
    "analysis_notes": "Krótkie podsumowanie znalezionych obszarów"
}}

Odpowiadaj tylko JSON.""",
    "en": """You are a UX/UI expert analyzing a transcript from an application review recording.

Analyze the ENTIRE transcript below and identify ALL moments where the user:
- Describes a bug, problem, or something that doesn't work
- Suggests a change or improvement
- Comments on UI/UX elements (buttons, forms, layout)
- Mentions performance issues
- Raises accessibility concerns
- Describes anything that may require developer attention

IMPORTANT: Be LIBERAL in identification - it's better to flag more moments than to miss potential issues.
The vision model will later verify each one by analyzing the screenshot.
{keywords_hint}
Transcript with timestamps:
{transcript_with_timestamps}

Respond in JSON format:
{{
    "points_of_interest": [
        {{
            "timestamp_start": 12.5,
            "timestamp_end": 18.0,
            "category": "bug|change|ui|performance|accessibility|other",
            "confidence": 0.85,
            "reasoning": "User says button doesn't respond to clicking",
            "transcript_excerpt": "this button here doesn't seem to work"
        }}
    ],
    "total_issues_found": 5,
    "analysis_notes": "Brief summary of identified areas"
}}

Respond only with JSON.""",
}


def get_semantic_prefilter_prompt(language: str = "en") -> str:
    """Get the semantic pre-filter prompt template for the specified language.

    The returned template still contains the ``{transcript_with_timestamps}``
    and ``{keywords_hint}`` placeholders; callers fill them in.
    """
    lang = language.lower().strip()
    if lang in ("pl", "pl-pl", "polish", "polski"):
        return SEMANTIC_PREFILTER_PROMPTS["pl"]
    return SEMANTIC_PREFILTER_PROMPTS["en"]


def format_transcript_with_timestamps(transcription: TranscriptionResult) -> str:
    """Format transcript with timestamps for LLM analysis."""
    lines = []
    for segment in transcription.segments:
        timestamp = f"[{segment.start:.1f}s - {segment.end:.1f}s]"
        lines.append(f"{timestamp} {segment.text}")
    return "\n".join(lines)


def semantic_prefilter(
    transcription: TranscriptionResult,
    config: ScreenScribeConfig,
    previous_response_id: str = "",
    keywords: KeywordsConfig | None = None,
) -> SemanticFilterResult:
    """
    Perform semantic pre-filtering on entire transcript.

    This analyzes the full transcript using LLM to identify points
    of interest BEFORE frame extraction, allowing more comprehensive
    analysis by the vision model.

    The active keyword dictionary (if any) is injected into the prompt as
    vocabulary *hints*: phrases the user/team uses to signal problem types.
    They are framed as a hint, not a rule -- the model still judges context,
    negation, and intent, and must not auto-create a finding just because a
    phrase appears. When no keywords are active the hint section is empty and
    the prompt behaves exactly as before (safe-when-empty).

    Args:
        transcription: Full transcription result with segments
        config: screenscribe configuration
        previous_response_id: Response ID from STT for conversation chaining
        keywords: Active keyword vocabulary hints (loaded by the caller). When
            ``None``, the built-in/global dictionary is loaded via the standard
            priority; an empty dictionary is a safe no-op.

    Returns:
        SemanticFilterResult with POIs and response_id for VLM context chaining
    """
    # A transcript with no usable text (e.g. every segment was a no-speech
    # hallucination removed by filter_hallucinated_segments) has nothing to
    # analyze. Short-circuit to zero findings instead of prompting the LLM on an
    # empty transcript -- that wastes a call and risks the model inventing a
    # finding from an empty prompt (FW-09).
    if not any(seg.text.strip() for seg in transcription.segments):
        console.print("[dim]No transcript text to analyze - skipping semantic pre-filter[/]")
        return SemanticFilterResult(pois=[], response_id=previous_response_id)

    if not config.get_llm_api_key():
        # The LLM detection stage is core; reaching it without a key is a
        # failure, not "zero findings". Surface it so the pipeline stops
        # honestly instead of emitting an empty "no issues" report.
        console.print("[red]No LLM API key configured - cannot run semantic pre-filter[/]")
        return SemanticFilterResult(
            pois=[], response_id="", failed=True, error="No LLM API key configured"
        )

    # Format transcript for analysis
    transcript_text = format_transcript_with_timestamps(transcription)

    # Build the vocabulary-hints section. Empty dict -> empty string -> no-op.
    if keywords is None:
        keywords = KeywordsConfig.load()
    keywords_hint = format_keywords_hint(keywords)
    if keywords_hint:
        keywords_hint = "\n" + keywords_hint + "\n"

    # Get localized prompt
    prompt_template = get_semantic_prefilter_prompt(config.language)
    prompt = prompt_template.format(
        transcript_with_timestamps=transcript_text,
        keywords_hint=keywords_hint,
    )
    prompt = apply_analysis_prompt_override(prompt, config.analysis_prompt_override)

    console.print("[blue]Running semantic pre-filter on entire transcript...[/]")

    if config.verbose:
        console.print(f"[dim]  Endpoint: {config.llm_endpoint}[/]")
        console.print(f"[dim]  Model: {config.llm_model}[/]")
        console.print(f"[dim]  Segments: {len(transcription.segments)}[/]")
        console.print(f"[dim]  Transcript length: {len(transcript_text)} chars[/]")

    try:
        # Build request with streaming enabled
        request_body = build_llm_request_body(config.llm_model, prompt, config.llm_endpoint)
        request_body["stream"] = True
        # Enable reasoning summaries in stream (for thinking models)
        request_body["reasoning"] = {"summary": "auto"}
        # Chain from STT response for thematic context
        if previous_response_id:
            request_body["previous_response_id"] = previous_response_id
            console.print(f"[dim]  Chaining from STT: {previous_response_id[:20]}...[/]")

        def _stream_prefilter_once() -> tuple[str, str]:
            """Run one full streaming attempt and return ``(content, response_id)``.

            Raises httpx errors (status / transport) on failure so the caller's
            ``retry_request`` can retry the *transient* ones (429/5xx/timeout/
            connect, honoring Retry-After). Each call starts from fresh local
            accumulators, so a retried attempt never duplicates or loses POIs
            streamed by an earlier partial attempt.
            """
            content = ""
            stream_preview = ""  # Last ~40 chars of output for live display
            reasoning_text = ""
            poi_count = 0
            response_id = ""  # Capture for conversation chaining to VLM

            # Create progress display with spinner + status + live stream
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description}"),
                BarColumn(bar_width=15),
                TextColumn("[dim]{task.fields[stream]}[/]"),
                transient=True,
            )

            with Live(progress, console=console, refresh_per_second=15):
                task_id = progress.add_task(
                    f"Analyzing {len(transcription.segments)} segments",
                    total=100,
                    stream="...",
                )

                with httpx.Client(timeout=120.0) as client:
                    with client.stream(
                        "POST",
                        config.llm_endpoint,
                        headers={
                            "Authorization": f"Bearer {config.get_llm_api_key()}",
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream",
                        },
                        json=request_body,
                    ) as response:
                        response.raise_for_status()

                        line_count = 0
                        for line in response.iter_lines():
                            line_count += 1
                            if not line:
                                continue

                            # Verbose SSE logging - outside Live context
                            # (disabled to not interfere with progress display)

                            # Handle SSE format: "event: xxx" or "data: xxx"
                            if line.startswith("event:"):
                                continue  # Skip event lines, we parse data

                            if line.startswith("data:"):
                                data = line[5:].strip()  # Strip "data:" prefix
                                if data == "[DONE]":
                                    progress.update(task_id, completed=100, stream="done")
                                    break

                                try:
                                    chunk = json.loads(data)

                                    # json.loads only guarantees valid JSON, not
                                    # the expected SHAPE. A valid-JSON-but-non-dict
                                    # chunk (list, number, bare string, null) makes
                                    # chunk.get(...) raise AttributeError, which is
                                    # NOT a JSONDecodeError, so it used to bubble to
                                    # the outer handler and turn a fully-streamed
                                    # prefilter into a hard stage failure (skipped
                                    # video). Skip the odd chunk, keep the stream
                                    # alive -- symmetric with analyze_one (C6.5).
                                    if not isinstance(chunk, dict):
                                        continue

                                    chunk_type = chunk.get("type", "")

                                    # Capture response_id for conversation chaining
                                    if chunk_type in ("response.created", "response.completed"):
                                        chunk_id = chunk.get("response", {}).get("id", "")
                                        if not chunk_id:
                                            chunk_id = chunk.get("id", "")
                                        if chunk_id:
                                            response_id = chunk_id

                                    # Show reasoning summaries in real-time
                                    if chunk_type == "response.reasoning_summary_text.delta":
                                        # Streaming reasoning summary delta
                                        delta = chunk.get("delta", "")
                                        if delta:
                                            reasoning_text = (reasoning_text + delta)[-60:]
                                            progress.update(task_id, stream=reasoning_text)
                                    elif chunk_type == "response.reasoning_summary_text.done":
                                        # Full reasoning summary completed
                                        full_text = chunk.get("text", "")
                                        if full_text:
                                            reasoning_text = full_text[-60:]
                                            progress.update(task_id, stream=reasoning_text)

                                    # Extract delta text from streaming response
                                    delta_text = _extract_stream_delta(chunk, verbose=False)
                                    if delta_text:
                                        content += delta_text
                                        # Update stream preview with last chars
                                        stream_preview = (stream_preview + delta_text)[-40:]
                                        # Clean for display (remove newlines, JSON noise)
                                        display_text = stream_preview.replace("\n", " ").replace(
                                            '"', ""
                                        )

                                        # Count POIs found so far
                                        new_poi_count = content.count('"timestamp_start"')
                                        if new_poi_count != poi_count:
                                            poi_count = new_poi_count
                                            progress.update(
                                                task_id,
                                                description=f"Found {poi_count} POI",
                                                completed=min(poi_count * 5, 95),
                                                stream=f"...{display_text}",
                                            )
                                        else:
                                            # Just update stream preview
                                            progress.update(task_id, stream=f"...{display_text}")

                                except (json.JSONDecodeError, AttributeError, TypeError):
                                    # JSONDecodeError: not valid JSON. Attribute/
                                    # TypeError: valid JSON with a wrong nested
                                    # shape (e.g. {"choices": [42]} ->
                                    # choices[0].get(...)) that the top-level
                                    # isinstance guard cannot catch. Skip this one
                                    # chunk; keep the stream alive.
                                    continue

            return content, response_id

        # Retry transient transport/HTTP failures (429/5xx/timeout/connect,
        # honoring Retry-After) before declaring the stage failed. Non-retriable
        # errors (401/403) fail fast, and exhausted retries propagate to the
        # except below -- both become a loud failed=True, never a silent empty.
        content, response_id = retry_request(
            _stream_prefilter_once,
            operation_name="Semantic pre-filter",
        )

        if not content:
            # A 200 OK that streamed no usable text is NOT a healthy empty result
            # (that would be a parseable `{"points_of_interest": []}`). The model
            # returned nothing -- fail loudly instead of reporting "no issues".
            console.print("[red]Empty response from semantic pre-filter[/]")
            return SemanticFilterResult(
                pois=[],
                response_id=response_id,
                failed=True,
                error="Empty response from semantic pre-filter",
            )

        # Parse JSON from content. strict=True so unparseable model output is
        # raised (-> failed=True below), not silently treated as zero findings.
        pois = _parse_prefilter_response(content, transcription, strict=True)

        console.print(
            f"[green]Semantic pre-filter complete:[/] identified {len(pois)} points of interest"
        )
        if response_id:
            console.print(f"[dim]  Response ID for VLM chaining: {response_id[:20]}...[/]")

        # Summary by category
        categories: dict[str, int] = {}
        for poi in pois:
            categories[poi.category] = categories.get(poi.category, 0) + 1

        for cat, count in sorted(categories.items()):
            console.print(f"[dim]  • {cat}: {count}[/]")

        return SemanticFilterResult(pois=pois, response_id=response_id)

    except Exception as e:
        # Auth (401), rate-limit (429), network drop, malformed stream -- any of
        # these must be reported as a FAILURE, never collapsed into an empty
        # result that the pipeline would render as a confident "no issues" report.
        console.print(f"[red]Semantic pre-filter failed: {e}[/]")
        return SemanticFilterResult(pois=[], response_id="", failed=True, error=str(e))


def _extract_stream_delta(chunk: dict[str, Any], verbose: bool = False) -> str:
    """Extract text delta from SSE streaming chunk.

    Supports Responses API streaming formats from OpenAI/LibraxisAI.
    """
    chunk_type = chunk.get("type", "")

    if verbose and chunk_type:
        console.print(f"[dim]  chunk type: {chunk_type}[/]")

    # Responses API: response.output_text.delta
    if chunk_type == "response.output_text.delta":
        return str(chunk.get("delta", ""))

    # Responses API: response.content_part.delta (alternative format)
    if chunk_type == "response.content_part.delta":
        delta = chunk.get("delta", {})
        if isinstance(delta, dict):
            return str(delta.get("text", ""))
        return str(delta) if delta else ""

    # Responses API: content.delta
    if chunk_type == "content.delta":
        delta = chunk.get("delta", {})
        if isinstance(delta, dict):
            return str(delta.get("text", ""))
        return str(delta) if delta else ""

    # Responses API: response.text.delta (yet another variant)
    if chunk_type == "response.text.delta":
        return str(chunk.get("delta", "") or chunk.get("text", ""))

    # Chat Completions API streaming format (legacy fallback)
    choices = chunk.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        return str(delta.get("content", ""))

    return ""


def _extract_content_from_response(result: dict[str, Any]) -> str:
    """Extract text content from API response."""
    content = ""
    for item in result.get("output", []):
        item_type = item.get("type", "")
        if item_type == "reasoning":
            pass  # Skip reasoning blocks
        elif item_type == "message":
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "text"):
                    content += part.get("text", "")
        elif item_type in ("output_text", "text"):
            content += item.get("text", "")
    return content


def _parse_prefilter_response(
    content: str, transcription: TranscriptionResult, *, strict: bool = False
) -> list[PointOfInterest]:
    """Parse the pre-filter response into PointOfInterest objects.

    With ``strict=True`` a total JSON parse failure is re-raised instead of
    being swallowed into ``[]``. Callers in the product path use this so that
    "the model returned unparseable garbage" is reported as a FAILURE, not as a
    genuine "no points of interest" result. (A successfully-parsed but empty
    list still returns ``[]`` -- that is a real empty, not a failure.)
    """
    # Strip model control tokens
    content = re.sub(r"<\|[^|]+\|>\w*\s*", "", content)

    # Find JSON in content
    if not content.strip().startswith("{"):
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            content = json_match.group(0)

    # Handle markdown code blocks
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        parts = content.split("```")
        if len(parts) >= 2:
            content = parts[1]

    content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        console.print(f"[yellow]Failed to parse pre-filter response: {e}[/]")
        if strict:
            raise
        return []

    # {"points_of_interest": null} is a benign "found nothing" the model emits as
    # valid JSON, and `.get(key, [])` returns None (not the default) when the key
    # is present with a null value -- `for item in None` would then TypeError and
    # abort the whole detection stage. Any non-list shape degrades to zero POIs.
    raw_pois = data.get("points_of_interest")
    if not isinstance(raw_pois, list):
        raw_pois = []

    pois = []
    for item in raw_pois:
        # A non-dict list entry (e.g. ["12s", {...}]) would make item.get(...)
        # raise AttributeError; skip it per-element instead of failing the run.
        if not isinstance(item, dict):
            continue
        timestamp_start = _coerce_timestamp(item.get("timestamp_start", 0.0))
        timestamp_end = _coerce_timestamp(item.get("timestamp_end", 0.0))
        # A missing/zero timestamp_end (with a real start) is degraded to a
        # point-like POI (end == start) instead of an inverted [start, 0] range
        # that produces an empty context and a nonsensical midpoint (BH21).
        if timestamp_end < timestamp_start:
            timestamp_end = timestamp_start

        # Find matching segment IDs by OVERLAP (not full containment): any
        # segment whose window intersects [start-1, end+1] contributes its id.
        # Full-containment dropped long/straddling segments that actually cover
        # the flagged moment (P2-4 / BH2 / BH19).
        segment_ids = [
            seg.id
            for seg in transcription.segments
            if _segments_overlap(seg.start, seg.end, timestamp_start - 1.0, timestamp_end + 1.0)
        ]

        poi = PointOfInterest(
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            category=_validate_poi_category(item.get("category", "other")),
            confidence=_coerce_confidence(item.get("confidence", 0.5)),
            # A present-but-null string field (``"reasoning": null``) survives
            # ``.get(key, "")`` as None and later breaks ``None.strip()`` in
            # deduplicate_pois; coerce to a real str at construction time.
            reasoning=str(item.get("reasoning") or ""),
            transcript_excerpt=str(item.get("transcript_excerpt") or ""),
            segment_ids=segment_ids,
        )
        pois.append(poi)

    return pois


def _segments_overlap(
    seg_start: float, seg_end: float, window_start: float, window_end: float
) -> bool:
    """True when [seg_start, seg_end] intersects [window_start, window_end].

    Overlap (not full containment): a segment that starts before the window or
    ends after it still contributes as long as the two intervals touch. This is
    the correct test for "does this segment cover the flagged moment".
    """
    return seg_start <= window_end and seg_end >= window_start


def _poi_similarity_text(poi: PointOfInterest) -> str:
    """Extract text for similarity comparison from a POI."""
    if poi.transcript_excerpt and poi.reasoning:
        return f"{poi.transcript_excerpt} {poi.reasoning}"
    if poi.transcript_excerpt:
        return poi.transcript_excerpt
    if poi.reasoning:
        return poi.reasoning
    return ""


def deduplicate_pois(
    pois: list[PointOfInterest],
    similarity_threshold: float = 0.45,
) -> list[PointOfInterest]:
    """Deduplicate similar POIs by transcript excerpt and reasoning.

    Groups POIs with similarity above threshold, then merges each group
    into a single POI with a merged time range and best confidence.

    Args:
        pois: List of PointOfInterest objects
        similarity_threshold: Minimum similarity (0-1) to consider as duplicate

    Returns:
        Deduplicated list of PointOfInterest objects
    """
    if not pois or len(pois) <= 1:
        return pois

    groups: list[list[PointOfInterest]] = []
    used: set[int] = set()

    for i, poi in enumerate(pois):
        if i in used:
            continue

        group = [poi]
        used.add(i)
        poi_text = _poi_similarity_text(poi)

        for j, other in enumerate(pois):
            if j in used:
                continue

            similarity = _text_similarity(poi_text, _poi_similarity_text(other))
            if similarity >= similarity_threshold:
                group.append(other)
                used.add(j)

        groups.append(group)

    result: list[PointOfInterest] = []

    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue

        group.sort(key=lambda p: p.timestamp_start)
        best = max(group, key=lambda p: p.confidence)

        # ``or ""`` guards a POI constructed directly with a null string field
        # (the parser now coerces, but direct callers may not) against
        # ``None.strip()`` mid-merge.
        excerpts = [
            (p.transcript_excerpt or "").strip()
            for p in group
            if (p.transcript_excerpt or "").strip()
        ]
        reasoning_parts = []
        seen_reasoning: set[str] = set()
        for p in group:
            if not p.reasoning:
                continue
            stripped_reasoning = p.reasoning.strip()
            key = stripped_reasoning.lower()
            if key in seen_reasoning:
                continue
            seen_reasoning.add(key)
            reasoning_parts.append(stripped_reasoning)

        merged = PointOfInterest(
            timestamp_start=min(p.timestamp_start for p in group),
            timestamp_end=max(p.timestamp_end for p in group),
            category=best.category,
            confidence=max(p.confidence for p in group),
            reasoning=" | ".join(reasoning_parts)
            if reasoning_parts
            else (group[0].reasoning or ""),
            transcript_excerpt=(
                max(excerpts, key=len) if excerpts else (group[0].transcript_excerpt or "")
            ),
            segment_ids=sorted({sid for p in group for sid in p.segment_ids}),
        )
        result.append(merged)

    return result


# Synthetic-id base for POIs that map to no transcript segment. Offsetting well
# above any plausible real segment id keeps the fallback ids from colliding with
# genuine segment ids while staying deterministic per POI position. Without a
# distinct id, every segment-less POI used segment.id=0 and downstream
# dedup/screenshot keys (detection_id, timestamp) collided (P2-4 / BH38).
_SYNTHETIC_POI_ID_BASE = 1_000_000


def poi_to_detection(
    poi: PointOfInterest,
    transcription: TranscriptionResult,
    detection_id: int | None = None,
) -> Any:
    """
    Convert a PointOfInterest to a Detection object for compatibility.

    This allows the semantic pre-filter to integrate with existing
    screenshot extraction and analysis pipeline.

    Args:
        poi: The point of interest to convert.
        transcription: Source transcription (for context segments).
        detection_id: Optional deterministic id for the synthetic segment. When
            omitted, the first real ``segment_ids`` entry is used, falling back
            to ``0`` only when the POI has no segment mapping. ``pois_to_detections``
            supplies a distinct id per segment-less POI so downstream keys stay
            discriminating.

    Returns:
        Detection object (typed as object to avoid circular import)
    """
    from .detect import Detection

    # Build context from any segment OVERLAPPING the POI window (+/-5s), not only
    # fully-contained ones — a long segment that spans the POI must still
    # contribute its text (P2-4 / BH2).
    context_segments = [
        seg.text
        for seg in transcription.segments
        if _segments_overlap(seg.start, seg.end, poi.timestamp_start - 5.0, poi.timestamp_end + 5.0)
    ]
    context = " ".join(context_segments)

    # An explicit detection_id is authoritative: ``pois_to_detections`` supplies a
    # de-collided, deterministic id per POI (real-id-preferring, but disambiguated
    # when two POIs share the same first segment id). Direct callers that omit it
    # keep the legacy real-segment-id behaviour, falling back to ``0``.
    if detection_id is not None:
        segment_id = detection_id
    elif poi.segment_ids:
        segment_id = poi.segment_ids[0]
    else:
        segment_id = 0

    # Create synthetic segment for the POI
    segment = Segment(
        id=segment_id,
        start=poi.timestamp_start,
        end=poi.timestamp_end,
        text=poi.transcript_excerpt,
    )

    # Preserve the POI category. Detection.category is a plain str, so there is
    # no need to collapse performance/accessibility/other to 'ui'; doing so
    # falsified the VLM prompt hint and the report category (BH44). Narrow only
    # to the validated POI vocabulary for safety.
    category = poi.category if poi.category in POI_CATEGORIES else "other"

    return Detection(
        segment=segment,
        category=category,
        keywords_found=[f"semantic:{poi.category}"],
        context=context,
    )


def pois_to_detections(
    pois: list[PointOfInterest], transcription: TranscriptionResult
) -> list[Any]:
    """Convert list of POIs to Detection objects with unique deterministic ids.

    Each detection_id is the key downstream merge/routing keys findings by, so it
    must be unique across the whole POI set. Two cases produced duplicates:

    * Segment-less POIs all defaulted to id ``0`` (P2-4 / BH38).
    * Two distinct POIs whose first ``segment_ids`` entry was identical both adopted
      that real id, e.g. ``id=18`` twice on review.mov (review-model-v2 cut E).

    Resolution: prefer the real first segment id when free, otherwise assign the
    deterministic synthetic fallback ``_SYNTHETIC_POI_ID_BASE + index`` (well above any
    plausible real id and unique per position). The same input yields the same ids.
    """
    detections = []
    used_ids: set[int] = set()
    for index, poi in enumerate(pois):
        preferred = poi.segment_ids[0] if poi.segment_ids else _SYNTHETIC_POI_ID_BASE + index
        if preferred in used_ids:
            # Real segment id already claimed by an earlier POI: fall back to the
            # position-derived synthetic id so the key stays discriminating.
            preferred = _SYNTHETIC_POI_ID_BASE + index
            while preferred in used_ids:  # defensive: keep the fallback unique too
                preferred += 1
        used_ids.add(preferred)
        detections.append(poi_to_detection(poi, transcription, detection_id=preferred))
    return detections
