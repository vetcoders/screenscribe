"""API utilities including retry logic with exponential backoff."""

import math
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from rich.console import Console
from rich.markup import escape

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


def endpoint_host(endpoint: str | None) -> str:
    """Return only the hostname of an endpoint URL (no scheme, userinfo, or query).

    Safe for user-facing messages: credentials embedded in a URL never leak.
    """
    if not endpoint:
        return "unknown host"
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        host = None
    return host or "unknown host"


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+")
_URL_TRAILING_PUNCTUATION = "'\")]},.;:"


def redact_url(url: str) -> str:
    """Return ``url`` safe to print: no userinfo, no query values, no fragment.

    Keeps scheme, host, port and path so the target stays recognizable.
    Userinfo becomes ``***@`` and every query value becomes ``***`` (keys are
    kept, e.g. ``?api-version=***&key=***``). Screenscribe never takes
    credentials from an endpoint URL (keys travel in the Authorization header);
    this is defense in depth for URLs echoed in logs and error messages.
    Unparseable input returns ``"unknown host"``, never the raw string.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except (ValueError, TypeError, AttributeError):
        return "unknown host"
    if not parts.scheme or not host:
        return "unknown host"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    if "@" in parts.netloc:
        netloc = f"***@{netloc}"
    # A bare query item (no "=") may itself be a token, so it is masked too.
    query = "&".join(
        f"{pair.split('=', 1)[0]}=***" if "=" in pair else "***"
        for pair in parts.query.split("&")
        if pair
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _redact_urls_in_text(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        stripped = raw.rstrip(_URL_TRAILING_PUNCTUATION)
        return redact_url(stripped) + raw[len(stripped) :]

    return _URL_IN_TEXT.sub(_replace, text)


def redact_error_message(error: BaseException) -> str:
    """``str(error)`` with every URL redacted (see ``redact_url``).

    httpx exceptions embed the request URL in their message (e.g. an
    HTTPStatusError's "for url '...'"). The exact request URL is replaced
    first, then any remaining ``http(s)://`` URL in the text is redacted too.
    """
    message = str(error)
    if isinstance(error, (httpx.RequestError, httpx.HTTPStatusError)):
        try:
            request_url = str(error.request.url)
        except RuntimeError:  # RequestError.request is unset
            request_url = ""
        if request_url:
            message = message.replace(request_url, redact_url(request_url))
    return _redact_urls_in_text(message)


# Substrings of a provider error code/type that mark a stream error as
# transient (worth retrying): overload, rate limit, timeouts, 5xx-style faults.
_TRANSIENT_STREAM_ERROR_MARKERS = (
    "server_error",
    "rate_limit",
    "too_many_requests",
    "overload",
    "capacity",
    "unavailable",
    "timeout",
    "timed_out",
    "bad_gateway",
    "gateway_timeout",
)


class StreamEventError(RuntimeError):
    """A provider error reported INSIDE an otherwise-200 SSE stream.

    Responses-API streams can fail after the HTTP status is already 200 by
    sending an ``error``, ``response.failed`` or ``response.incomplete`` event.
    ``raise_for_status`` never sees those, so they are raised as this dedicated
    exception. ``is_retriable_error`` retries it when ``transient`` is True, so
    ``retry_request`` handles an in-stream overload exactly like an HTTP 503.
    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` handlers
    keep catching provider error events.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        event_type: str = "",
        transient: bool = False,
    ) -> None:
        detail = message
        if code and code not in message:
            detail = f"{message} (code: {code})"
        super().__init__(detail)
        self.code = code
        self.event_type = event_type
        self.transient = transient


def _stream_error_fields(payload: Any) -> tuple[str, str]:
    """Pull ``(message, code)`` from an error payload that may be a dict or str."""
    if isinstance(payload, str):
        return payload.strip(), ""
    if not isinstance(payload, dict):
        return "", ""
    message = payload.get("message")
    code = payload.get("code") or payload.get("type") or payload.get("status") or ""
    return (
        message.strip() if isinstance(message, str) else "",
        str(code).strip() if code is not None else "",
    )


def _is_transient_stream_error(code: str, message: str) -> bool:
    if code.isdigit():
        return int(code) in RETRIABLE_STATUS_CODES
    haystack = f"{code} {message}".lower().replace(" ", "_").replace("-", "_")
    return any(marker in haystack for marker in _TRANSIENT_STREAM_ERROR_MARKERS)


def extract_stream_error_event(chunk: dict[str, Any]) -> StreamEventError | None:
    """Return the provider error carried by one SSE chunk, or ``None``.

    Recognized shapes (Responses API and compatible providers):

    - ``{"type": "error", "error": {...}}`` or ``{"type": "error", "code", "message"}``
    - ``{"type": "response.failed", "response": {"error": {...}}}``
    - ``{"type": "response.completed"|"response.done", "response": {"status": "failed"}}``
    - ``{"type": "response.incomplete", "response": {"incomplete_details": {"reason"}}}``
    - an untyped JSON body with a top-level ``error`` (a non-SSE error reply).

    Every recognized shape is a terminal failure of the response: callers must
    not accept partial content that streamed before it. ``event_type`` names the
    event so messages can say which kind of failure it was.
    """
    chunk_type = str(chunk.get("type", "") or "")

    if chunk_type == "error" or (not chunk_type and chunk.get("error")):
        message, code = _stream_error_fields(chunk.get("error"))
        top_message, top_code = _stream_error_fields(
            {"message": chunk.get("message"), "code": chunk.get("code")}
        )
        message = message or top_message
        code = code or top_code
        message = message or "Streaming provider returned an error event."
        return StreamEventError(
            message,
            code=code,
            event_type=chunk_type or "error",
            transient=_is_transient_stream_error(code, message),
        )

    response_payload = chunk.get("response")
    if not isinstance(response_payload, dict):
        response_payload = {}
    status = str(response_payload.get("status", "") or "")

    if chunk_type == "response.failed" or (
        chunk_type in ("response.completed", "response.done") and status == "failed"
    ):
        message, code = _stream_error_fields(response_payload.get("error"))
        if not message:
            message = (
                "Response failed."
                if chunk_type == "response.failed"
                else "Streaming response completed with failed status."
            )
        return StreamEventError(
            message,
            code=code,
            event_type=chunk_type,
            transient=_is_transient_stream_error(code, message),
        )

    if chunk_type == "response.incomplete":
        details = response_payload.get("incomplete_details")
        reason = ""
        if isinstance(details, dict) and details.get("reason"):
            reason = str(details["reason"]).strip()
        message = f"Response incomplete: {reason}" if reason else "Response incomplete."
        return StreamEventError(
            message,
            code=reason,
            event_type=chunk_type,
            transient=_is_transient_stream_error(reason, ""),
        )

    return None


_TEXT_OUTPUT_EVENT_TYPES = (
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.delta",
    "content.delta",
    "response.text.delta",
)


def stream_chunk_has_model_output(chunk: dict[str, Any]) -> bool:
    """Whether an SSE chunk carries model output (a reasoning or text delta).

    Used to decide if an in-stream provider error is still worth retrying: a
    failure that arrives before the model produced anything is treated like an
    HTTP 5xx, but once the model has reasoned or answered, re-running the whole
    (possibly many-minute) generation is not transient -- fail fast instead.
    """
    chunk_type = str(chunk.get("type", "") or "")
    if chunk_type.startswith("response.reasoning") and chunk_type.endswith((".delta", ".done")):
        return True
    if chunk_type in _TEXT_OUTPUT_EVENT_TYPES:
        return True
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        return isinstance(delta, dict) and bool(delta.get("content"))
    return False


def responses_reasoning_options(endpoint: str, effort: str) -> dict[str, str] | None:
    """``reasoning`` block for a text-LLM request, or ``None`` for Chat Completions.

    Responses API requests ask for streamed reasoning summaries AND an explicit
    effort; Chat Completions endpoints do not accept this block at all.
    """
    if is_chat_completions_endpoint(endpoint):
        return None
    return {"summary": "auto", "effort": effort}


def extract_response_payload_error(result: dict[str, Any]) -> StreamEventError | None:
    """Provider failure carried by a NON-streaming 200 response body, or ``None``.

    A Responses API call can return HTTP 200 with ``status: "failed"`` (and an
    ``error`` object) or ``status: "incomplete"`` (``incomplete_details.reason``,
    e.g. a reasoning model that exhausted its budget). Either way any text in
    ``output`` is not a finished answer and must not be used as one. Reuses the
    stream-event parser so both paths report the same message and code.
    """
    if not isinstance(result, dict):
        return None
    status = str(result.get("status", "") or "").strip().lower()
    if status == "failed":
        return extract_stream_error_event({"type": "response.failed", "response": result})
    if status == "incomplete":
        return extract_stream_error_event({"type": "response.incomplete", "response": result})
    error_payload = result.get("error")
    if error_payload:
        return extract_stream_error_event({"type": "error", "error": error_payload})
    return None


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

    # A provider error event inside a 200 stream: retry only transient ones
    # (overload / rate limit / server fault), never e.g. invalid_prompt.
    if isinstance(error, StreamEventError):
        return error.transient

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
            console.print(f"[dim]  Error: {escape(redact_error_message(e))}[/]")

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
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Build request body for either Responses API or Chat Completions API.

    Args:
        model: Model name
        prompt: Text prompt
        endpoint: API endpoint URL (used to detect format)
        image_base64: Optional base64-encoded image for vision
        reasoning_effort: When set, Responses API bodies get a ``reasoning``
            block with this effort (see ``responses_reasoning_options``);
            Chat Completions bodies never do. Text-LLM callers pass
            ``config.get_llm_reasoning_effort()`` so reasoning models cannot
            loop without an answer on long prompts.

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
        body: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": input_content}],
        }
        if reasoning_effort:
            reasoning = responses_reasoning_options(endpoint, reasoning_effort)
            if reasoning is not None:
                body["reasoning"] = reasoning
        return body


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
