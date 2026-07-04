"""On-the-wire payload building and SSE decoding for unified analysis.

Covers both the chat-completions and the responses API shapes: request
payload construction plus the streaming chunk decoders.

NOTE: ``_extract_stream_delta`` here returns a ``tuple[str, bool]``. A
same-named helper in ``screenscribe.semantic_filter`` returns ``str`` with a
different signature — these are intentionally distinct and must not be merged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..api_utils import is_chat_completions_endpoint
from ..image_utils import encode_image_base64, get_media_type
from ._console import console


def _build_unified_payload(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    screenshot_path: Path | None,
    previous_response_id: str | None,
    stream: bool,
    same_provider: bool = True,
) -> dict[str, object]:
    """Build a unified analysis payload for either chat or responses endpoints.

    ``same_provider`` guards conversation chaining: ``previous_response_id`` is
    only attached when the vision and LLM endpoints belong to the same provider.
    Screenscribe allows independent vision/LLM endpoints, and a response id minted
    by one provider is meaningless (or an error) when replayed against another.
    """
    use_chat_completions = is_chat_completions_endpoint(endpoint)
    has_screenshot = screenshot_path is not None and screenshot_path.exists()

    if use_chat_completions:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if has_screenshot and screenshot_path:
            image_base64 = encode_image_base64(screenshot_path)
            media_type = get_media_type(screenshot_path)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{image_base64}"},
                }
            )
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        if stream:
            payload["stream"] = True
        return payload

    content_responses: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if has_screenshot and screenshot_path:
        image_b64 = encode_image_base64(screenshot_path)
        media_type = get_media_type(screenshot_path)
        content_responses.append(
            {
                "type": "input_image",
                "image_url": f"data:{media_type};base64,{image_b64}",
            }
        )

    payload = {
        "model": model,
        "input": [{"role": "user", "content": content_responses}],
        "reasoning": {"summary": "auto"},
    }
    # Only chain when vision and LLM endpoints are the same provider.
    if previous_response_id and same_provider:
        payload["previous_response_id"] = previous_response_id
    if stream:
        payload["stream"] = True
    return payload


def _extract_stream_delta(chunk: dict[str, Any], verbose: bool = False) -> tuple[str, bool]:
    """Extract text delta from SSE streaming chunk.

    Supports Responses API streaming formats from OpenAI/LibraxisAI.

    Returns:
        Tuple of (text, is_final_text). If is_final_text is True, the emitted text
        should replace any partially collected stream to avoid duplicate output.
    """
    chunk_type = chunk.get("type", "")

    if verbose and chunk_type:
        console.print(f"[dim]  chunk type: {chunk_type}[/]")

    # Responses API: response.output_text.delta
    if chunk_type == "response.output_text.delta":
        return str(chunk.get("delta", "")), False

    # Responses API: final text block without prior deltas
    if chunk_type == "response.output_text.done":
        return str(chunk.get("text", "")), True

    # Responses API: response.content_part.delta (alternative format)
    if chunk_type == "response.content_part.delta":
        delta = chunk.get("delta", {})
        if isinstance(delta, dict):
            return str(delta.get("text", "")), False
        return (str(delta) if delta else "", False)

    # Responses API: content.delta
    if chunk_type == "content.delta":
        delta = chunk.get("delta", {})
        if isinstance(delta, dict):
            return str(delta.get("text", "")), False
        return (str(delta) if delta else "", False)

    # Responses API: response.text.delta (yet another variant)
    if chunk_type == "response.text.delta":
        return str(chunk.get("delta", "") or chunk.get("text", "")), False

    # Chat Completions API streaming format (legacy fallback)
    choices = chunk.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        return str(delta.get("content", "")), False

    return "", False


def _extract_stream_error(chunk: dict[str, Any]) -> str:
    """Extract provider-side stream error message from SSE chunk."""
    chunk_type = str(chunk.get("type", ""))
    if chunk_type == "error":
        error_payload = chunk.get("error", {})
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return "Streaming provider returned an error event."

    if chunk_type in ("response.completed", "response.done"):
        response_payload = chunk.get("response", {})
        if isinstance(response_payload, dict):
            if response_payload.get("status") == "failed":
                error_payload = response_payload.get("error", {})
                if isinstance(error_payload, dict):
                    message = error_payload.get("message")
                    if isinstance(message, str) and message.strip():
                        return message.strip()
                return "Streaming response completed with failed status."

    return ""


def _extract_reasoning_delta(chunk: dict[str, Any]) -> str:
    """Extract reasoning summary delta from SSE chunk."""
    chunk_type = chunk.get("type", "")

    if chunk_type == "response.reasoning_summary_text.delta":
        return str(chunk.get("delta", ""))
    elif chunk_type == "response.reasoning_summary_text.done":
        return str(chunk.get("text", ""))

    return ""


def _extract_response_id_from_stream(chunk: dict[str, Any]) -> str:
    """Extract response ID from streaming chunk."""
    chunk_type = chunk.get("type", "")

    # Responses-style stream chunks often carry the canonical response id
    # inside the nested response object, not at top level.
    if chunk_type in ("response.created", "response.completed", "response.done"):
        response = chunk.get("response", {})
        if isinstance(response, dict):
            return str(response.get("id", ""))

    # Some formats include ID at chunk level
    return str(chunk.get("id", "") or chunk.get("response_id", ""))
