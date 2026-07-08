"""API utilities including retry logic with exponential backoff."""

import math
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from rich.console import Console

console = Console()

T = TypeVar("T")

# Upper bound on how long we honor a server-advertised Retry-After. Respecting
# the header stops us from hammering a rate-limited endpoint on our own short
# backoff, while the cap prevents an absurd value from stalling the run.
RETRY_AFTER_MAX_SECONDS = 120.0

# Status codes that should trigger a retry
RETRIABLE_STATUS_CODES = {
    408,  # Request Timeout
    429,  # Too Many Requests (rate limit)
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
}

AUTH_FAILURE_STATUS_CODES = {401, 403}


class APIError(Exception):
    """API request error with details."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def retry_after_seconds(error: Exception) -> float | None:
    """Return the server-advertised Retry-After delay in seconds, if present.

    Handles the delta-seconds form (e.g. ``Retry-After: 5``). The HTTP-date form
    is not parsed; callers fall back to exponential backoff in that case.
    """
    response = getattr(error, "response", None)
    if response is None:
        return None
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    # A non-finite Retry-After (``inf``/``nan``/``-inf``) must not become the
    # delay: +Inf would stall forever and NaN only "works" by the accident of
    # max(0.0, nan)==0.0. Reject it so the caller falls back to exponential
    # backoff (retry_after_seconds == None branch).
    if not math.isfinite(value):
        return None
    return max(0.0, value)


def is_retriable_error(error: Exception) -> bool:
    """Check if an error should trigger a retry."""
    # Timeout errors are always retriable
    if isinstance(error, httpx.TimeoutException):
        return True

    # Transport/network drops are transient. A concurrency-capped STT endpoint
    # tears connections mid-upload, which surfaces as ReadError/WriteError/
    # RemoteProtocolError rather than a clean ConnectError -- none of which the
    # old ConnectError-only check retried, so a single mid-transfer drop killed
    # the whole transcription. httpx.NetworkError covers ConnectError/ReadError/
    # WriteError/CloseError; RemoteProtocolError (peer spoke malformed HTTP or
    # closed the stream early) is a sibling worth retrying. LocalProtocolError is
    # a client-side bug, not transient, so it stays non-retriable (it is a
    # ProtocolError but NOT a RemoteProtocolError, so this check excludes it).
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return True

    # HTTP status errors - check the status code
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in AUTH_FAILURE_STATUS_CODES:
            return False
        return status_code in RETRIABLE_STATUS_CODES

    return False


def retry_request(
    fn: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    operation_name: str = "API request",
) -> T:
    """
    Execute a function with exponential backoff retry.

    Args:
        fn: Function to execute (should raise httpx exceptions on failure)
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        operation_name: Name of operation for logging

    Returns:
        Result of fn()

    Raises:
        The last exception if all retries fail
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e

            # Check if this error is retriable
            if not is_retriable_error(e):
                # Non-retriable error (e.g., 400, 401, 404) - fail immediately
                raise

            # Check if we have retries left
            if attempt >= max_retries:
                console.print(f"[red]{operation_name} failed after {max_retries + 1} attempts[/]")
                raise

            # Prefer the server's Retry-After when it tells us how long to wait
            # (e.g. a 429 rate limit); otherwise use exponential backoff + jitter.
            retry_after = retry_after_seconds(e)
            if retry_after is not None:
                delay = min(retry_after, RETRY_AFTER_MAX_SECONDS)
            else:
                delay = min(base_delay * (2**attempt), max_delay)
                # Add jitter to prevent thundering herd
                import random

                delay = delay * (0.5 + random.random())  # noqa: S311

            console.print(
                f"[yellow]{operation_name} failed (attempt {attempt + 1}/{max_retries + 1}), "
                f"retrying in {delay:.1f}s...[/]"
            )
            console.print(f"[dim]  Error: {e}[/]")

            time.sleep(delay)

    # This shouldn't happen, but just in case
    if last_error:
        raise last_error
    raise RuntimeError("Unexpected retry loop exit")


def make_api_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    operation_name: str = "API request",
    **kwargs: Any,
) -> httpx.Response:
    """
    Make an HTTP request with automatic retry on transient failures.

    Args:
        client: httpx.Client instance
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        max_retries: Maximum retry attempts
        operation_name: Name for logging
        **kwargs: Additional arguments passed to client.request()

    Returns:
        httpx.Response on success

    Raises:
        httpx.HTTPStatusError: On non-retriable HTTP errors
        httpx.TimeoutException: If all retries fail due to timeout
    """

    def do_request() -> httpx.Response:
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    return retry_request(do_request, max_retries=max_retries, operation_name=operation_name)


def is_chat_completions_endpoint(endpoint: str) -> bool:
    """Check if endpoint uses Chat Completions API format.

    Responses API (/v1/responses) is the new standard for both OpenAI and LibraxisAI.
    Only use Chat Completions format if the endpoint explicitly contains 'chat/completions'.
    """
    return "chat/completions" in endpoint


def build_llm_request_body(
    model: str,
    prompt: str,
    endpoint: str,
    image_base64: str | None = None,
) -> dict[str, Any]:
    """Build request body for either Responses API or Chat Completions API.

    Args:
        model: Model name
        prompt: Text prompt
        endpoint: API endpoint URL (used to detect format)
        image_base64: Optional base64-encoded image for vision

    Returns:
        Request body dict
    """
    if is_chat_completions_endpoint(endpoint):
        # OpenAI Chat Completions format
        content: str | list[dict[str, Any]]
        if image_base64:
            content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                },
            ]
        else:
            content = prompt
        return {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
    else:
        # Responses API format (OpenAI + LibraxisAI)
        if image_base64:
            input_content: list[dict[str, Any]] = [
                {"type": "input_text", "text": prompt},
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{image_base64}",
                },
            ]
        else:
            input_content = [{"type": "input_text", "text": prompt}]
        return {
            "model": model,
            "input": [{"role": "user", "content": input_content}],
        }


def extract_llm_response_text(response_json: dict[str, Any], endpoint: str) -> str:
    """Extract text content from LLM response (either API format).

    Args:
        response_json: Parsed JSON response
        endpoint: API endpoint URL (used to detect format)

    Returns:
        Extracted text content
    """
    if is_chat_completions_endpoint(endpoint):
        # OpenAI Chat Completions format
        choices = response_json.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            return content if isinstance(content, str) else ""
        return ""
    else:
        # LibraxisAI Responses API format
        direct_text = response_json.get("output_text")
        if isinstance(direct_text, str):
            return direct_text
        if isinstance(direct_text, dict):
            direct_text_value = direct_text.get("text", "")
            if isinstance(direct_text_value, str):
                return direct_text_value
        if isinstance(direct_text, list):
            direct_parts: list[str] = []
            for text_part in direct_text:
                if not isinstance(text_part, dict):
                    continue
                text_value = text_part.get("text")
                if isinstance(text_value, str):
                    direct_parts.append(text_value)
            if direct_parts:
                return "".join(direct_parts)

        text_value = response_json.get("text")
        if isinstance(text_value, str):
            return text_value

        content_value = response_json.get("content")
        if isinstance(content_value, str):
            return content_value

        content = ""
        output_items = response_json.get("output", [])
        if not isinstance(output_items, list):
            return content

        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type == "reasoning":
                # Skip reasoning blocks
                pass
            elif item_type == "message":
                message_parts = item.get("content", [])
                if not isinstance(message_parts, list):
                    continue
                for part in message_parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("output_text", "text"):
                        text = part.get("text", "")
                        content += text if isinstance(text, str) else ""
            elif item_type in ("output_text", "text"):
                text = item.get("text", "")
                content += text if isinstance(text, str) else ""
        return content
