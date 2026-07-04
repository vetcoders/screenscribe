"""Extra coverage for semantic_filter.py uncovered paths.

Targets the streaming semantic_prefilter body, _extract_stream_delta format
variants, markdown-fence parsing, _poi_similarity_text, and deduplicate_pois.
All LLM/httpx I/O is mocked; assertions pin real behavior, not call-and-pass.
"""

import json
import math
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Literal

import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.semantic_filter import (
    PointOfInterest,
    SemanticFilterResult,
    _coerce_confidence,
    _coerce_timestamp,
    _extract_stream_delta,
    _parse_prefilter_response,
    _poi_similarity_text,
    deduplicate_pois,
    poi_to_detection,
    pois_to_detections,
    semantic_prefilter,
)
from screenscribe.transcribe import Segment, TranscriptionResult

# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="The button does not work. We should fix it. Layout looks fine.",
        segments=[
            Segment(id=0, start=0.0, end=3.0, text="The button does not work."),
            Segment(id=1, start=3.5, end=6.0, text="We should fix it."),
            Segment(id=2, start=7.0, end=10.0, text="Layout looks fine."),
        ],
        language="en",
    )


def _poi(
    ts: float,
    te: float,
    *,
    category: str = "bug",
    confidence: float = 0.8,
    reasoning: str = "",
    excerpt: str = "",
    segment_ids: list[int] | None = None,
) -> PointOfInterest:
    return PointOfInterest(
        timestamp_start=ts,
        timestamp_end=te,
        category=category,  # type: ignore[arg-type]
        confidence=confidence,
        reasoning=reasoning,
        transcript_excerpt=excerpt,
        segment_ids=segment_ids if segment_ids is not None else [],
    )


# --- Mock streaming httpx client ------------------------------------------


class _FakeStreamResponse:
    """Yields a scripted list of SSE lines from semantic_prefilter's POST."""

    def __init__(self, lines: list[str], raise_exc: Exception | None = None) -> None:
        self._lines = lines
        self._raise_exc = raise_exc

    def __enter__(self) -> "_FakeStreamResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines


def _make_client(lines: list[str], raise_exc: Exception | None = None) -> type:
    """Build a fake httpx.Client class that streams the given SSE lines."""

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
            return _FakeStreamResponse(lines, raise_exc=raise_exc)

    return _FakeClient


def _empty_poi_delta_line() -> str:
    """SSE data line carrying an output_text delta with an empty points_of_interest payload."""
    chunk = {
        "type": "response.output_text.delta",
        "delta": json.dumps({"points_of_interest": []}),
    }
    return f"data: {json.dumps(chunk)}"


def _config(**overrides: Any) -> ScreenScribeConfig:
    base: dict[str, Any] = {
        "llm_api_key": "test-key",  # pragma: allowlist secret
        "llm_endpoint": "https://api.example.com/v1/responses",
        "llm_model": "test-model",
        "language": "en",
    }
    base.update(overrides)
    return ScreenScribeConfig(**base)


# --- semantic_prefilter: no API key ---------------------------------------


def test_prefilter_no_api_key_returns_empty(transcription: TranscriptionResult) -> None:
    """Without an LLM key the prefilter short-circuits to an empty result (lines 197-198)."""
    config = _config(llm_api_key="")
    result = semantic_prefilter(transcription, config)
    assert isinstance(result, SemanticFilterResult)
    assert result.pois == []
    assert result.response_id == ""


# --- semantic_prefilter: full streaming happy path ------------------------


def test_prefilter_streams_and_parses_pois(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
) -> None:
    """A scripted SSE stream produces parsed POIs and captures the response_id.

    Exercises response.created id capture, reasoning summary deltas,
    output_text deltas, POI-count progression and the final JSON parse.
    """
    poi_json = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": "bug",
                    "confidence": 0.9,
                    "reasoning": "button broken",
                    "transcript_excerpt": "The button does not work.",
                },
                {
                    "timestamp_start": 7.0,
                    "timestamp_end": 10.0,
                    "category": "ui",
                    "confidence": 0.6,
                    "reasoning": "layout comment",
                    "transcript_excerpt": "Layout looks fine.",
                },
            ]
        }
    )

    lines = [
        "event: response.created",
        f"data: {json.dumps({'type': 'response.created', 'response': {'id': 'resp_abc123'}})}",
        # reasoning summary delta -> stream preview update
        f"data: {json.dumps({'type': 'response.reasoning_summary_text.delta', 'delta': 'thinking'})}",
        # reasoning summary done -> full text branch
        f"data: {json.dumps({'type': 'response.reasoning_summary_text.done', 'text': 'done reasoning'})}",
        # output text delta carrying the JSON payload (split to drive poi counter)
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': poi_json[:60]})}",
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': poi_json[60:]})}",
        "data: [DONE]",
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _make_client(lines))

    config = _config()
    result = semantic_prefilter(transcription, config)

    assert result.response_id == "resp_abc123"
    assert len(result.pois) == 2
    categories = {p.category for p in result.pois}
    assert categories == {"bug", "ui"}
    # segment ids were matched against the transcription time ranges
    bug_poi = next(p for p in result.pois if p.category == "bug")
    assert 0 in bug_poi.segment_ids


def test_prefilter_verbose_and_chaining(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
) -> None:
    """Verbose config + previous_response_id exercise lines 211-214, 224-225.

    Also covers the response.completed id fallback (chunk.id when response.id absent).
    """
    captured: dict[str, Any] = {}

    class _CapturingFakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_CapturingFakeClient":
            return self

        def __exit__(self, *a: Any) -> Literal[False]:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResponse:
            captured["body"] = kwargs["json"]
            completed = json.dumps({"type": "response.completed", "id": "resp_top_level"})
            text_delta = json.dumps(
                {
                    "type": "response.output_text.delta",
                    "delta": json.dumps({"points_of_interest": []}),
                }
            )
            lines = [
                # completed event with top-level id (response.id empty -> fallback to chunk id)
                f"data: {completed}",
                f"data: {text_delta}",
                "data: [DONE]",
            ]
            return _FakeStreamResponse(lines)

    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _CapturingFakeClient)

    config = _config(verbose=True)
    result = semantic_prefilter(transcription, config, previous_response_id="prev_stt_id_123456789")

    assert result.response_id == "resp_top_level"
    assert result.pois == []
    # chaining injected previous_response_id into the request body
    assert captured["body"]["previous_response_id"] == "prev_stt_id_123456789"


def test_prefilter_empty_content_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
) -> None:
    """A stream with no text deltas yields empty content -> empty pois (lines 335-337)."""
    lines = [
        f"data: {json.dumps({'type': 'response.created', 'response': {'id': 'resp_empty'}})}",
        "data: [DONE]",
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _make_client(lines))

    result = semantic_prefilter(transcription, _config())
    assert result.pois == []
    # response_id is still propagated even when content is empty
    assert result.response_id == "resp_empty"


def test_prefilter_skips_blank_and_event_lines(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
) -> None:
    """Blank lines and 'event:' lines are skipped without breaking parse (lines 265-273)."""
    lines = [
        "",  # blank -> continue (line 266)
        "event: foo",  # event line -> continue
        _empty_poi_delta_line(),
        "data: [DONE]",
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _make_client(lines))

    result = semantic_prefilter(transcription, _config())
    assert result.pois == []


def test_prefilter_ignores_malformed_json_chunk(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
) -> None:
    """A data line with invalid JSON is swallowed (JSONDecodeError continue, line 332-333)."""
    lines = [
        "data: {not valid json",
        _empty_poi_delta_line(),
        "data: [DONE]",
    ]
    monkeypatch.setattr("screenscribe.semantic_filter.httpx.Client", _make_client(lines))

    result = semantic_prefilter(transcription, _config())
    assert result.pois == []


def test_prefilter_http_error_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    transcription: TranscriptionResult,
) -> None:
    """raise_for_status raising is caught -> empty result (except branch, lines 358-360)."""
    monkeypatch.setattr(
        "screenscribe.semantic_filter.httpx.Client",
        _make_client([], raise_exc=RuntimeError("boom 500")),
    )

    result = semantic_prefilter(transcription, _config())
    assert result.pois == []
    assert result.response_id == ""


# --- _extract_stream_delta variants ---------------------------------------


def test_extract_delta_output_text() -> None:
    """response.output_text.delta returns the delta string (line 375)."""
    assert _extract_stream_delta({"type": "response.output_text.delta", "delta": "hi"}) == "hi"


def test_extract_delta_content_part_dict() -> None:
    """response.content_part.delta with dict delta returns nested text (lines 379-382)."""
    chunk = {"type": "response.content_part.delta", "delta": {"text": "abc"}}
    assert _extract_stream_delta(chunk) == "abc"


def test_extract_delta_content_part_non_dict() -> None:
    """response.content_part.delta with non-dict delta is stringified."""
    chunk = {"type": "response.content_part.delta", "delta": "plain"}
    assert _extract_stream_delta(chunk) == "plain"


def test_extract_delta_content_delta_dict() -> None:
    """content.delta with dict delta returns nested text (lines 386-389)."""
    chunk = {"type": "content.delta", "delta": {"text": "xyz"}}
    assert _extract_stream_delta(chunk) == "xyz"


def test_extract_delta_response_text_delta() -> None:
    """response.text.delta prefers delta then text (line 393)."""
    assert _extract_stream_delta({"type": "response.text.delta", "text": "fallback"}) == "fallback"
    assert _extract_stream_delta({"type": "response.text.delta", "delta": "primary"}) == "primary"


def test_extract_delta_chat_completions_fallback() -> None:
    """Legacy choices[].delta.content path is used when no Responses type matches (398-399)."""
    chunk = {"choices": [{"delta": {"content": "legacy"}}]}
    assert _extract_stream_delta(chunk) == "legacy"


def test_extract_delta_unknown_returns_empty() -> None:
    """An unrecognized chunk yields empty string (final return)."""
    assert _extract_stream_delta({"type": "response.something.else"}) == ""


def test_extract_delta_verbose_logs(capsys: pytest.CaptureFixture[str]) -> None:
    """verbose=True prints the chunk type for diagnostics (line 371)."""
    _extract_stream_delta({"type": "response.output_text.delta", "delta": "x"}, verbose=True)
    out = capsys.readouterr().out
    assert "chunk type" in out


# --- _parse_prefilter_response markdown fences ----------------------------


def test_parse_json_fenced_block(transcription: TranscriptionResult) -> None:
    """A ```json fenced block is unwrapped before parsing (line 435).

    The content starts with '{' so the leading-JSON regex is skipped, letting the
    ```json fence-split branch actually run on the real payload that follows.
    """
    payload = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": "bug",
                    "confidence": 0.7,
                    "reasoning": "r",
                    "transcript_excerpt": "e",
                }
            ]
        }
    )
    content = "{}preamble```json\n" + payload + "\n```"
    pois = _parse_prefilter_response(content, transcription)
    assert len(pois) == 1
    assert pois[0].category == "bug"


def test_parse_plain_fenced_block(transcription: TranscriptionResult) -> None:
    """A plain ``` fence (no json tag) is unwrapped via parts[1] (lines 437-439)."""
    inner = json.dumps({"points_of_interest": []})
    content = "{}preamble```\n" + inner + "\n```"
    pois = _parse_prefilter_response(content, transcription)
    assert pois == []


def test_parse_invalid_json_returns_empty(transcription: TranscriptionResult) -> None:
    """Unparseable JSON content yields an empty POI list (JSONDecodeError, lines 445-447)."""
    content = "{ this is not valid json at all"
    pois = _parse_prefilter_response(content, transcription)
    assert pois == []


# --- _poi_similarity_text -------------------------------------------------


def test_similarity_text_both_fields() -> None:
    """Both excerpt and reasoning -> concatenated (line 474-475)."""
    poi = _poi(0, 1, reasoning="why", excerpt="what")
    assert _poi_similarity_text(poi) == "what why"


def test_similarity_text_excerpt_only() -> None:
    """Only excerpt -> excerpt (lines 476-477)."""
    poi = _poi(0, 1, reasoning="", excerpt="what")
    assert _poi_similarity_text(poi) == "what"


def test_similarity_text_reasoning_only() -> None:
    """Only reasoning -> reasoning (lines 478-479)."""
    poi = _poi(0, 1, reasoning="why", excerpt="")
    assert _poi_similarity_text(poi) == "why"


def test_similarity_text_neither() -> None:
    """Neither field -> empty string (line 480)."""
    poi = _poi(0, 1, reasoning="", excerpt="")
    assert _poi_similarity_text(poi) == ""


# --- deduplicate_pois -----------------------------------------------------


def test_dedup_empty_passthrough() -> None:
    """Empty list returns unchanged (line 499-500)."""
    assert deduplicate_pois([]) == []


def test_dedup_single_passthrough() -> None:
    """A single POI is returned as-is."""
    pois = [_poi(0, 1, excerpt="solo")]
    assert deduplicate_pois(pois) == pois


def test_dedup_distinct_pois_unmerged() -> None:
    """Dissimilar POIs stay separate (each is its own group, len-1 branch)."""
    pois = [
        _poi(0, 1, excerpt="the login button is completely broken", reasoning="auth fails"),
        _poi(50, 51, excerpt="chart colors should use the brand palette", reasoning="theming"),
    ]
    result = deduplicate_pois(pois, similarity_threshold=0.9)
    assert len(result) == 2


def test_dedup_merges_similar_pois() -> None:
    """Two near-identical POIs merge into one with union time range and best confidence."""
    pois = [
        _poi(
            10.0,
            12.0,
            category="bug",
            confidence=0.6,
            reasoning="The save button does not respond",
            excerpt="save button broken",
            segment_ids=[1],
        ),
        _poi(
            11.0,
            14.0,
            category="ui",
            confidence=0.9,
            reasoning="The save button does not respond at all",
            excerpt="save button broken when clicked",
            segment_ids=[2],
        ),
    ]
    result = deduplicate_pois(pois, similarity_threshold=0.3)
    assert len(result) == 1
    merged = result[0]
    # union of the time range
    assert merged.timestamp_start == 10.0
    assert merged.timestamp_end == 14.0
    # best (max) confidence kept, and category comes from the highest-confidence POI
    assert merged.confidence == 0.9
    assert merged.category == "ui"
    # segment ids merged + sorted
    assert merged.segment_ids == [1, 2]
    # longest excerpt wins
    assert merged.transcript_excerpt == "save button broken when clicked"
    # distinct reasonings joined with separator
    assert " | " in merged.reasoning


def test_dedup_merges_dedupes_identical_reasoning() -> None:
    """Identical reasoning (case-insensitive) is not duplicated in the merged text."""
    pois = [
        _poi(0, 2, confidence=0.5, reasoning="Same Reason", excerpt="aaa bbb ccc"),
        _poi(1, 3, confidence=0.8, reasoning="same reason", excerpt="aaa bbb ccc ddd"),
    ]
    result = deduplicate_pois(pois, similarity_threshold=0.3)
    assert len(result) == 1
    # only one reasoning kept despite two POIs (case-folded dedupe)
    assert result[0].reasoning == "Same Reason"


def test_dedup_merge_falls_back_to_group_reasoning_when_all_blank() -> None:
    """When no POI in a merged group has reasoning, fall back to group[0].reasoning."""
    pois = [
        _poi(0, 2, confidence=0.5, reasoning="", excerpt="alpha beta gamma delta"),
        _poi(1, 3, confidence=0.7, reasoning="", excerpt="alpha beta gamma delta epsilon"),
    ]
    result = deduplicate_pois(pois, similarity_threshold=0.3)
    assert len(result) == 1
    assert result[0].reasoning == ""
    # excerpt fallback picks the longest
    assert result[0].transcript_excerpt == "alpha beta gamma delta epsilon"


# --- BH53: confidence float coercion --------------------------------------


def test_parse_coerces_string_confidence_to_float(
    transcription: TranscriptionResult,
) -> None:
    """A numeric-string confidence is coerced to float at parse time (BH53)."""
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": "bug",
                    "confidence": "0.85",
                    "reasoning": "r",
                    "transcript_excerpt": "e",
                }
            ]
        }
    )
    pois = _parse_prefilter_response(content, transcription)
    assert len(pois) == 1
    assert isinstance(pois[0].confidence, float)
    assert pois[0].confidence == 0.85


def test_parse_defaults_non_numeric_confidence(
    transcription: TranscriptionResult,
) -> None:
    """A non-numeric confidence string falls back to the default float (BH53)."""
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": "bug",
                    "confidence": "high",
                    "reasoning": "r",
                    "transcript_excerpt": "e",
                }
            ]
        }
    )
    pois = _parse_prefilter_response(content, transcription)
    assert len(pois) == 1
    assert isinstance(pois[0].confidence, float)
    assert pois[0].confidence == 0.5


def test_dedup_survives_mixed_string_and_float_confidence(
    transcription: TranscriptionResult,
) -> None:
    """Parsing then deduplicating mixed str/float confidences must not raise (BH53).

    Before coercion, deduplicate_pois' max() over a str/float group raised
    TypeError. Two text-similar POIs (one string, one numeric confidence) must
    merge cleanly with a numeric winner.
    """
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": "bug",
                    "confidence": "0.9",
                    "reasoning": "the save button does not respond at all",
                    "transcript_excerpt": "save button broken",
                },
                {
                    "timestamp_start": 1.0,
                    "timestamp_end": 4.0,
                    "category": "bug",
                    "confidence": 0.6,
                    "reasoning": "the save button does not respond",
                    "transcript_excerpt": "save button broken when clicked",
                },
            ]
        }
    )
    pois = _parse_prefilter_response(content, transcription)
    merged = deduplicate_pois(pois, similarity_threshold=0.3)
    assert len(merged) == 1
    assert merged[0].confidence == 0.9


# --- P2-4 / BH2 / BH19: overlap (not containment) segment matching --------


def _overlap_transcription() -> TranscriptionResult:
    """A single long STT segment spanning the whole clip (sparse-segment STT)."""
    return TranscriptionResult(
        text="one long spoken segment covering everything",
        segments=[Segment(id=7, start=2.0, end=40.0, text="long enclosing segment")],
        language="en",
    )


def test_parse_segment_ids_include_straddling_segment() -> None:
    """A segment that encloses the POI window contributes its id (P2-4 / BH19).

    With full-containment matching the enclosing segment (2.0-40.0) was dropped
    for a tight POI window, leaving segment_ids empty.
    """
    transcription = _overlap_transcription()
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 12.5,
                    "timestamp_end": 18.0,
                    "category": "bug",
                    "confidence": 0.8,
                    "reasoning": "r",
                    "transcript_excerpt": "e",
                }
            ]
        }
    )
    pois = _parse_prefilter_response(content, transcription)
    assert len(pois) == 1
    assert pois[0].segment_ids == [7]


def test_poi_to_detection_context_from_enclosing_segment() -> None:
    """An enclosing segment's text is included in the VLM context (P2-4 / BH2).

    Full-containment matching produced an empty context for a tight POI inside a
    long segment.
    """
    transcription = _overlap_transcription()
    poi = _poi(12.5, 18.0, category="bug", excerpt="e", segment_ids=[7])
    detection = poi_to_detection(poi, transcription)
    assert "long enclosing segment" in detection.context


# --- P2-4 / BH38: deterministic discriminating detection ids --------------


def test_pois_to_detections_segmentless_get_distinct_ids() -> None:
    """Segment-less POIs receive distinct deterministic ids, not all 0 (P2-4 / BH38).

    Two POIs sharing the same timestamp_start with no segment mapping must not
    collapse to the same (segment.id, segment.start) key.
    """
    transcription = TranscriptionResult(text="", segments=[], language="en")
    pois = [
        _poi(5.0, 6.0, category="bug", excerpt="first event"),
        _poi(5.0, 6.0, category="ui", excerpt="second event"),
    ]
    detections = pois_to_detections(pois, transcription)
    ids = [d.segment.id for d in detections]
    assert len(set(ids)) == 2, f"ids collided: {ids}"
    keys = {(d.segment.id, d.segment.start) for d in detections}
    assert len(keys) == 2


def test_pois_to_detections_prefer_real_segment_id() -> None:
    """When a POI has real segment_ids, the first real id wins over the synthetic one."""
    transcription = TranscriptionResult(
        text="x",
        segments=[Segment(id=3, start=4.0, end=8.0, text="real segment")],
        language="en",
    )
    pois = [_poi(5.0, 6.0, category="bug", excerpt="e", segment_ids=[3])]
    detections = pois_to_detections(pois, transcription)
    assert detections[0].segment.id == 3


def test_pois_to_detections_shared_real_segment_id_unique() -> None:
    """Two POIs whose first real segment id is identical must not collide (cut E).

    Reproduces the review.mov ``id=18`` twice collision: the synthetic-base mechanism
    only de-collided segment-less POIs, so two distinct POIs that both adopt the same
    first ``segment_ids`` entry produced duplicate ``detection_id`` in report findings
    — poisoning the merge/routing key in review-model-v2. detection_id must stay unique
    AND deterministic at source.
    """
    transcription = TranscriptionResult(
        text="x",
        segments=[
            Segment(id=18, start=148.2, end=152.2, text="one"),
            Segment(id=19, start=152.7, end=158.2, text="two"),
        ],
        language="pl",
    )
    pois = [
        _poi(148.2, 152.2, category="change", excerpt="first", segment_ids=[18]),
        _poi(152.7, 158.2, category="change", excerpt="second", segment_ids=[18, 19]),
    ]
    detections = pois_to_detections(pois, transcription)
    ids = [d.segment.id for d in detections]
    assert len(set(ids)) == len(ids), f"detection ids collided: {ids}"
    # prefer-real-id contract preserved for the first occurrence
    assert ids[0] == 18
    # determinism: identical input yields identical ids (no random/uuid)
    again = [d.segment.id for d in pois_to_detections(pois, transcription)]
    assert ids == again


# --- BH44: POI category preserved through detection ------------------------


def test_poi_to_detection_preserves_performance_category() -> None:
    """performance/accessibility/other survive conversion (BH44)."""
    transcription = TranscriptionResult(text="", segments=[], language="en")
    for category in ("performance", "accessibility", "other"):
        poi = _poi(0.0, 1.0, category=category, excerpt="e")
        detection = poi_to_detection(poi, transcription)
        assert detection.category == category


# --- BH21: missing/zero timestamp_end degrades to point-like POI ----------


def test_parse_zero_timestamp_end_degraded_to_point(
    transcription: TranscriptionResult,
) -> None:
    """A zero/absent timestamp_end with a real start becomes point-like (BH21).

    An inverted [12.5, 0.0] range yields a misleading midpoint (6.25) and empty
    context; degrading to end == start keeps the POI anchored at the real moment.
    """
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 12.5,
                    "category": "bug",
                    "confidence": 0.8,
                    "reasoning": "r",
                    "transcript_excerpt": "e",
                }
            ]
        }
    )
    pois = _parse_prefilter_response(content, transcription)
    assert len(pois) == 1
    poi = pois[0]
    assert poi.timestamp_end == poi.timestamp_start == 12.5
    # midpoint now points at the real moment, not the inverted-range artifact
    assert poi.midpoint == 12.5


# --- C6.4: non-finite (NaN/Inf) coercion guard ----------------------------


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_coerce_confidence_rejects_non_finite_number(raw: float) -> None:
    """A1: NaN/Inf/-Inf numbers coerce to the default, not a poison float."""
    assert _coerce_confidence(raw) == 0.5


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "Infinity", "-Infinity", "NaN"])
def test_coerce_confidence_rejects_non_finite_string(raw: str) -> None:
    """A2: string forms of NaN/Inf (which float() happily parses) hit the guard."""
    assert _coerce_confidence(raw) == 0.5


@pytest.mark.parametrize("raw", [float("inf"), float("nan"), "nan", "inf", "-Infinity"])
def test_coerce_timestamp_rejects_non_finite(raw: object) -> None:
    """A3: non-finite timestamps (number or string) coerce to the default."""
    assert _coerce_timestamp(raw) == 0.0


def test_coerce_finite_values_unchanged() -> None:
    """A4: the guard does not regress legitimate finite inputs."""
    assert _coerce_confidence(0.85) == 0.85
    assert _coerce_confidence("0.85") == 0.85
    assert _coerce_confidence(True) == 0.5  # bool guard intact
    assert _coerce_timestamp("12.5") == 12.5
    assert _coerce_timestamp(7) == 7.0


def test_non_finite_confidence_does_not_poison_dedup_ranking() -> None:
    """A6: a model-supplied Infinity confidence (coerced to 0.5) must not win
    the deduplicate_pois max(...) ranking nor leak a non-finite value out."""
    # Two near-identical POIs in the same 30s window so dedup groups them.
    poisoned = _poi(
        0.0,
        2.0,
        confidence=_coerce_confidence(float("inf")),  # -> 0.5
        excerpt="The save button does nothing when clicked",
    )
    genuine = _poi(
        1.0,
        3.0,
        confidence=_coerce_confidence(0.95),  # real high-confidence finding
        excerpt="The save button does nothing when clicked",
    )

    result = deduplicate_pois([poisoned, genuine], similarity_threshold=0.3)

    # Every surviving confidence is finite (no NaN/Inf leaked through).
    assert all(math.isfinite(p.confidence) for p in result)
    # The genuine 0.95 finding wins ranking; the +Inf->0.5 one did not dominate.
    assert max(p.confidence for p in result) == 0.95
