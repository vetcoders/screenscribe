"""Single-finding unified analysis transport (streaming + non-streaming).

Holds the per-finding VLM call path: prompt assembly, payload build, HTTP
transport with retry, and the text-only fallback recursion. The fallback
recursion between the streaming and non-streaming variants stays intra-module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from ..api_utils import retry_request
from ..config import ScreenScribeConfig
from ..detect import Detection
from ..keywords import format_keywords_hint
from ..prompts import apply_analysis_prompt_override, get_unified_analysis_prompt
from ._console import console
from .finding import UnifiedFinding
from .response_parsing import (
    _build_unified_finding,
    _extract_response_error,
    extract_response_content,
    parse_json_response,
)
from .wire import (
    _build_unified_payload,
    _extract_reasoning_delta,
    _extract_response_id_from_stream,
    _extract_stream_delta,
    _extract_stream_error,
)


def _may_chain_previous_response(
    config: ScreenScribeConfig, *, use_text_only_backend: bool
) -> bool:
    """Decide whether previous_response_id may be replayed on this call.

    A response id minted by one provider is meaningless (or an error) when
    replayed against another, so chaining is gated by provider identity.

    When the vision and LLM endpoints are the same provider, any id is
    replayable (historical behavior). When they differ (split-provider), the
    orchestrator still chains the VISION conversation: the preflight probe and
    every screenshot-backed finding hit the vision endpoint, so the chained id is
    vision-minted. A vision-backed call may therefore replay it (vision->vision
    chaining), while a text-only LLM-backed call must not (true cross-provider
    handoff). The previous gate (endpoints-equal only) dropped the id for EVERY
    split-provider vision request, losing screenshot-to-screenshot chaining (F).
    """
    if config.vision_endpoint == config.llm_endpoint:
        return True
    return not use_text_only_backend


def _build_keywords_hint_block(config: ScreenScribeConfig) -> str:
    """Format the active keyword vocabulary as a hint block for the analyze prompt.

    The unified prompts carry a ``{keywords_hint}`` placeholder on its own line.
    When the active dictionary is empty the formatted hint is an empty string and
    this returns ``""`` so the placeholder collapses to a blank line (safe no-op).
    Otherwise the hint is wrapped in surrounding newlines so it sits as its own
    block, matching the semantic prefilter convention.

    Keywords here do NOT find the moment (the human marked it); they only help
    the model interpret the user's comment/voice note, still judging context,
    negation, and intent.
    """
    hint = format_keywords_hint(config.get_keywords())
    if not hint:
        return ""
    return "\n" + hint + "\n"


def analyze_finding_unified_streaming(
    detection: Detection,
    screenshot_path: Path | None,
    config: ScreenScribeConfig,
    previous_response_id: str | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    on_content: Callable[[str], None] | None = None,
    force_text_only: bool = False,
) -> UnifiedFinding | None:
    """
    Analyze a single finding using VLM with streaming SSE response.

    This is the streaming version that provides real-time feedback
    via callbacks for reasoning and content deltas.

    Args:
        detection: The detection to analyze
        screenshot_path: Path to screenshot (can be None if extraction failed)
        config: screenscribe configuration
        previous_response_id: Response ID from previous finding for context chaining
        on_reasoning: Callback for reasoning summary deltas
        on_content: Callback for content deltas

    Returns:
        UnifiedFinding result or None if analysis failed
    """
    has_screenshot = screenshot_path is not None and screenshot_path.exists()
    use_text_only_backend = force_text_only or not has_screenshot
    api_key = config.get_llm_api_key() if use_text_only_backend else config.get_vision_api_key()
    endpoint = config.llm_endpoint if use_text_only_backend else config.vision_endpoint
    model = config.llm_model if use_text_only_backend else config.vision_model

    if not api_key:
        # The relevant API key for this backend is missing. We keep returning
        # None because the orchestrator guards this upstream
        # (analyze_all_findings_unified returns early when no vision key) and the
        # text-only fallback recursion below relies on None as a "skip" signal.
        # But a *direct* caller would otherwise get a silent None that is
        # indistinguishable from a genuine "no finding" -- a quiet analyze-side
        # fail-open. Warn loudly so the missing-key cause is visible.
        backend = "LLM" if use_text_only_backend else "vision"
        console.print(
            f"[yellow]Unified analyze skipped: no {backend} API key configured "
            "(returning no finding for this item).[/]"
        )
        return None

    # Get appropriate prompt (with or without image)
    prompt_template = get_unified_analysis_prompt(config.language, text_only=use_text_only_backend)

    # Build prompt with FULL context. Active keywords are injected as vocabulary
    # hints so the model interprets the user's comment with the team's phrasing in
    # mind; empty dictionary -> empty hint -> no-op.
    prompt = prompt_template.format(
        transcript_context=detection.segment.text,
        full_context=detection.context,
        category=detection.category,
        keywords_hint=_build_keywords_hint_block(config),
    )
    prompt = apply_analysis_prompt_override(prompt, config.analysis_prompt_override)

    try:
        payload = _build_unified_payload(
            endpoint=endpoint,
            model=model,
            prompt=prompt,
            screenshot_path=None if use_text_only_backend else screenshot_path,
            previous_response_id=previous_response_id,
            stream=True,
            same_provider=_may_chain_previous_response(
                config, use_text_only_backend=use_text_only_backend
            ),
        )

        # Tracks whether a *prior* attempt already forwarded stream deltas to the
        # consumer, so a retry does not feed it the same prefix twice (see the
        # _run_stream docstring). Lives in the enclosing scope because
        # retry_request re-invokes _run_stream across attempts.
        emitted = False

        def _run_stream() -> tuple[str, str]:
            """Run one full streaming attempt -> ``(collected_content, response_id)``.

            Wrapped in ``retry_request`` so a transient 429/5xx/transport drop on
            the image-backed call is retried (honoring Retry-After) BEFORE
            degrading to text-only, instead of discarding the visual signal on the
            first blip -- the provider is known to cap concurrency and return 429.

            A retriable status error (429/5xx) is raised by ``raise_for_status()``
            before the body is read, so no delta was emitted yet and the retry is
            clean. But a retriable *transport* drop (ReadTimeout / NetworkError /
            RemoteProtocolError) can fire mid-body, AFTER some ``on_content`` /
            ``on_reasoning`` deltas were already forwarded. Re-running from the top
            would replay that prefix and duplicate it for the consumer. To prevent
            that, once any delta has been forwarded (``emitted``), later attempts
            suppress the callbacks -- ``collected_content`` is still rebuilt from
            scratch and returned correctly, only the live re-emission is dropped. A
            provider error-event (RuntimeError) is non-retriable and propagates
            straight to the outer fallback, matching the previous behavior.
            """
            nonlocal emitted
            collected_content = ""
            response_id = ""

            # A retry that follows a mid-stream drop must not re-forward the
            # prefix the failed attempt already streamed to the consumer.
            suppress_callbacks = emitted

            def emit_content(text: str) -> None:
                nonlocal emitted
                if suppress_callbacks:
                    return
                if on_content:
                    emitted = True
                    on_content(text)

            def emit_reasoning(text: str) -> None:
                nonlocal emitted
                if suppress_callbacks:
                    return
                if on_reasoning:
                    emitted = True
                    on_reasoning(text)

            with httpx.Client(timeout=120.0) as client:
                with client.stream(
                    "POST",
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json=payload,
                ) as response:
                    response.raise_for_status()

                    for line in response.iter_lines():
                        if not line:
                            continue

                        # Handle SSE format
                        if line.startswith("event:"):
                            continue

                        if line.startswith("data:"):
                            line_data = line[5:].strip()
                            if line_data == "[DONE]":
                                break

                            try:
                                chunk = json.loads(line_data)

                                # C6.5: json.loads only guarantees valid JSON, not
                                # the expected SHAPE. A valid-JSON-but-non-dict chunk
                                # (a list, number, bare string, null) would make every
                                # helper's chunk.get(...) raise AttributeError/
                                # TypeError, which is NOT a JSONDecodeError, so it used
                                # to bubble to the outer handler and abort the WHOLE
                                # stream (losing already-collected deltas). Skip the
                                # malformed chunk and keep the stream alive instead.
                                if not isinstance(chunk, dict):
                                    continue

                                stream_error = _extract_stream_error(chunk)
                                if stream_error:
                                    raise RuntimeError(stream_error)

                                # Extract response ID FIRST, before any content
                                # reconciliation. The canonical id often rides on the
                                # same response.completed/response.done chunk that also
                                # carries the final content; reconciling content first
                                # used to `continue` past this extraction when the
                                # final content was not longer than the collected
                                # deltas, silently dropping the id (BH57).
                                chunk_response_id = _extract_response_id_from_stream(chunk)
                                if chunk_response_id:
                                    response_id = chunk_response_id

                                # Extract reasoning delta
                                reasoning_delta = _extract_reasoning_delta(chunk)
                                if reasoning_delta:
                                    emit_reasoning(reasoning_delta)

                                # Extract content delta
                                content_delta, is_final_text = _extract_stream_delta(chunk)
                                if content_delta:
                                    if is_final_text and collected_content:
                                        # Some providers emit the full final text
                                        # after a delta stream. is_final_text is the
                                        # provider's source-of-truth assertion, so
                                        # honor it even when the final text is NOT
                                        # longer than the deltas we already
                                        # accumulated -- instead of silently dropping
                                        # it (BH7). Only emit a positive tail to the
                                        # callback (we cannot un-emit already-streamed
                                        # text), but always reconcile collected_content
                                        # to the authoritative final text.
                                        if len(content_delta) > len(collected_content):
                                            emit_content(content_delta[len(collected_content) :])
                                        collected_content = content_delta
                                    else:
                                        collected_content += content_delta
                                        emit_content(content_delta)

                                # Some Responses-compatible providers deliver the
                                # final content only in response.completed/done.
                                if chunk.get("type") in (
                                    "response.completed",
                                    "response.done",
                                ):
                                    response_payload = chunk.get("response", {})
                                    if isinstance(response_payload, dict):
                                        final_content = extract_response_content(
                                            response_payload,
                                            endpoint=endpoint,
                                        )
                                        if final_content:
                                            if len(final_content) <= len(collected_content):
                                                continue
                                            previous_content_length = len(collected_content)
                                            collected_content = final_content
                                            emit_content(final_content[previous_content_length:])

                            except (json.JSONDecodeError, AttributeError, TypeError):
                                # JSONDecodeError: not valid JSON. AttributeError/
                                # TypeError: valid JSON whose nested shape is wrong
                                # (e.g. {"choices": [42]} -> choices[0].get(...)), a
                                # shape-error the top-level isinstance guard cannot
                                # catch. Skip this one chunk; keep the stream alive.
                                # A provider error-event raises RuntimeError, which is
                                # deliberately NOT caught here so it still propagates.
                                continue

            return collected_content, response_id

        # Retry transient transport/HTTP failures (429/5xx/timeout/network,
        # honoring Retry-After) on the image-backed call before degrading. A
        # non-retriable error (400/401/403 or a provider RuntimeError) and an
        # exhausted retry both propagate to the except below -> text-only /
        # non-streaming fallback, exactly as before.
        collected_content, response_id = retry_request(
            _run_stream,
            operation_name=f"Unified streaming analysis ({detection.segment.start:.1f}s)",
        )

        if not collected_content:
            if has_screenshot and not use_text_only_backend:
                if config.verbose:
                    console.print(
                        "[dim]Screenshot-backed unified stream returned no content; "
                        "retrying text-only unified analysis...[/]"
                    )
                return analyze_finding_unified_streaming(
                    detection,
                    screenshot_path,
                    config,
                    previous_response_id=previous_response_id,
                    on_reasoning=on_reasoning,
                    on_content=on_content,
                    force_text_only=True,
                )
            if config.verbose:
                console.print(
                    "[dim]Streaming unified analysis returned no content; retrying non-streaming...[/]"
                )
            return analyze_finding_unified(
                detection,
                screenshot_path,
                config,
                previous_response_id=previous_response_id,
                force_text_only=use_text_only_backend,
            )

        # Match non-streaming behavior: tolerate non-JSON model output and
        # keep the raw content instead of silently dropping the finding.
        data = parse_json_response(collected_content)
        return _build_unified_finding(detection, screenshot_path, data, response_id)

    except Exception as e:
        if has_screenshot and not use_text_only_backend:
            if config.verbose:
                console.print(f"[dim]Streaming image-backed analysis failed: {e}[/]")
                console.print("[dim]Retrying unified analysis without image...[/]")
            return analyze_finding_unified_streaming(
                detection,
                screenshot_path,
                config,
                previous_response_id=previous_response_id,
                on_reasoning=on_reasoning,
                on_content=on_content,
                force_text_only=True,
            )
        if config.verbose:
            console.print(f"[dim]Streaming analysis failed: {e}[/]")
            console.print("[dim]Retrying unified analysis without streaming...[/]")
        return analyze_finding_unified(
            detection,
            screenshot_path,
            config,
            previous_response_id=previous_response_id,
            force_text_only=use_text_only_backend,
        )


def analyze_finding_unified(
    detection: Detection,
    screenshot_path: Path | None,
    config: ScreenScribeConfig,
    previous_response_id: str | None = None,
    force_text_only: bool = False,
) -> UnifiedFinding | None:
    """
    Analyze a single finding using VLM with both image and full context.

    This is the core function that replaces separate semantic + vision analysis.
    It sends the screenshot AND full transcript context to VLM in a single call.

    Args:
        detection: The detection to analyze
        screenshot_path: Path to screenshot (can be None if extraction failed)
        config: screenscribe configuration
        previous_response_id: Response ID from previous finding for context chaining

    Returns:
        UnifiedFinding result or None if analysis failed
    """
    has_screenshot = screenshot_path is not None and screenshot_path.exists()
    use_text_only_backend = force_text_only or not has_screenshot
    api_key = config.get_llm_api_key() if use_text_only_backend else config.get_vision_api_key()
    endpoint = config.llm_endpoint if use_text_only_backend else config.vision_endpoint
    model = config.llm_model if use_text_only_backend else config.vision_model

    if not api_key:
        console.print("[yellow]No Vision API key - skipping unified analysis[/]")
        return None

    # Get appropriate prompt (with or without image)
    prompt_template = get_unified_analysis_prompt(config.language, text_only=use_text_only_backend)

    # Build prompt with FULL context (not just 200 chars!). Active keywords are
    # injected as vocabulary hints so the model interprets the user's comment with
    # the team's phrasing in mind; empty dictionary -> empty hint -> no-op.
    prompt = prompt_template.format(
        transcript_context=detection.segment.text,
        full_context=detection.context,  # Full context from surrounding segments
        category=detection.category,
        keywords_hint=_build_keywords_hint_block(config),
    )
    prompt = apply_analysis_prompt_override(prompt, config.analysis_prompt_override)

    try:

        def do_unified_request() -> httpx.Response:
            with httpx.Client(timeout=120.0) as client:
                payload = _build_unified_payload(
                    endpoint=endpoint,
                    model=model,
                    prompt=prompt,
                    screenshot_path=None if use_text_only_backend else screenshot_path,
                    previous_response_id=previous_response_id,
                    stream=False,
                    same_provider=_may_chain_previous_response(
                        config, use_text_only_backend=use_text_only_backend
                    ),
                )

                response = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                return response

        response = retry_request(
            do_unified_request,
            max_retries=3,
            operation_name=f"Unified analysis ({detection.segment.start:.1f}s)",
        )

        # Parse response
        raw_text = response.text
        if not raw_text or raw_text.strip() == "":
            console.print(f"[yellow]Empty response from API (status {response.status_code})[/]")
            return None

        try:
            result = response.json()
        except Exception as e:
            console.print(f"[yellow]Failed to parse API response: {e}[/]")
            return None

        response_error = _extract_response_error(result)
        if response_error:
            raise RuntimeError(response_error)

        # Extract content from response (supports both API formats)
        content_text = extract_response_content(result, endpoint=endpoint)

        if not content_text:
            if has_screenshot and not use_text_only_backend:
                if config.verbose:
                    console.print(
                        "[dim]Screenshot-backed unified response returned no content; "
                        "retrying text-only unified analysis...[/]"
                    )
                return analyze_finding_unified(
                    detection,
                    screenshot_path,
                    config,
                    previous_response_id=previous_response_id,
                    force_text_only=True,
                )
            console.print(
                f"[yellow]No content in response. Output types: "
                f"{[i.get('type') for i in result.get('output', [])]}[/]"
            )
            return None

        # Parse JSON from content
        data = parse_json_response(content_text)
        if "parse_error" in data:
            console.print(
                f"[yellow]JSON parse error: {data['parse_error']}. "
                f"Content (truncated): {data.get('raw_content', '')[:200]}...[/]"
            )

        # Extract response_id for conversation chaining
        response_id = result.get("id", "")

        return _build_unified_finding(detection, screenshot_path, data, response_id)

    except Exception as e:
        if has_screenshot and not use_text_only_backend:
            if config.verbose:
                console.print(f"[dim]Unified image-backed analysis failed: {e}[/]")
                console.print("[dim]Retrying unified analysis without image...[/]")
            return analyze_finding_unified(
                detection,
                screenshot_path,
                config,
                previous_response_id=previous_response_id,
                force_text_only=True,
            )
        console.print(f"[yellow]Unified analysis failed: {e}[/]")
        return None
