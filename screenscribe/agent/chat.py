"""Provider routing, Responses/Anthropic calls, tool loop, SSE contract.

The donor (family-onko-portal ``ai_chat.py``) is ~1790 lines of medical chat.
This module keeps the same contracts — PRIMARY/FALLBACK, trust
local/internal/processor/external, Responses ``previous_response_id``, Anthropic tools,
streaming, egress deny-by-default — and reimplements the loop without PubMed,
dossiers, or abstract translation.

SSE (binding for w1-05)::

    event: token        data: {"text": ...}
    event: tool_call    data: {"name", "input"}
    event: tool_result  data: {"name", "result"}
    event: done         data: {"response_id", "provider", "protocol", "host"}
    event: error        data: {"message": ...}

``tool_result.result`` may be a ``review_patch`` or ``review_plan`` object from
the write tools. The SSE envelope is unchanged; the browser applies the patch.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field, StringConstraints

from ..api_utils import (
    extract_stream_error_event,
    is_chat_completions_endpoint,
    redact_error_message,
    responses_reasoning_options,
)
from ..config import ScreenScribeConfig
from .context import PreparedTurn, prepare_turn
from .tools import ReportToolbelt, anthropic_tool_schemas, responses_tool_schemas

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 8
_MAX_MESSAGE_CHARS = 16_000
_MAX_HISTORY_ITEMS = 50
_MAX_HISTORY_FIELD_CHARS = 16_000
_INTERNAL_HOSTS = {"api.libraxis.cloud"}
_TRUST_LEVELS = {"local", "internal", "processor", "external"}
_KEPT_UNDER_DENY = {"local", "internal", "processor"}

# Tests replace this to avoid the network.
RoundTripper = Callable[["AgentProvider", dict[str, Any]], Awaitable["ProviderRound"]]
round_tripper: RoundTripper | None = None
BoundedHistoryField = Annotated[str, StringConstraints(max_length=_MAX_HISTORY_FIELD_CHARS)]


class AgentChatError(Exception):
    """Agent turn failed after every provider (or before any could run)."""


class AgentChatRequest(BaseModel):
    """POST /api/agent/chat and /api/agent/chat/stream body."""

    message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_CHARS)
    history: list[dict[str, BoundedHistoryField]] = Field(
        default_factory=list, max_length=_MAX_HISTORY_ITEMS
    )
    previous_response_id: str | None = Field(default=None, max_length=512)
    previous_response_provider: str | None = Field(default=None, max_length=32)
    previous_response_protocol: str | None = Field(default=None, max_length=32)
    previous_response_host: str | None = Field(default=None, max_length=255)


@dataclass(frozen=True)
class AgentProvider:
    name: str
    protocol: str  # "responses" | "anthropic" | "chat_completions"
    key: str
    model: str
    url: str
    trust: str
    slot: str
    host: str


@dataclass
class FunctionCall:
    name: str
    call_id: str
    arguments: dict[str, Any]


@dataclass
class ProviderRound:
    text: str
    response_id: str | None
    function_calls: list[FunctionCall]
    error: str | None = None
    tool_use_blocks: list[dict[str, Any]] = field(default_factory=list)
    response_output_items: list[dict[str, Any]] = field(default_factory=list)


def format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def normalize_agent_egress(value: str | None) -> str:
    raw = (value or "deny").strip().lower()
    return "allow" if raw == "allow" else "deny"


def analysis_hosts(
    config: ScreenScribeConfig,
    processing_provenance: dict[str, Any] | None = None,
) -> frozenset[str]:
    """Configured agent host only when a persisted semantic receipt proves it.

    Legacy reports and configured-but-unused endpoints are unproven. They remain
    external under ``deny`` unless the operator supplies an explicit trust
    override.
    """
    if not isinstance(processing_provenance, dict):
        return frozenset()
    receipt = processing_provenance.get("llm")
    if not isinstance(receipt, dict):
        return frozenset()
    host = receipt.get("host")
    protocol = receipt.get("protocol")
    provider = receipt.get("provider")
    if not isinstance(host, str) or not host.strip():
        return frozenset()
    if not isinstance(protocol, str) or not protocol.strip():
        return frozenset()
    if not isinstance(provider, str) or not provider.strip():
        return frozenset()
    host = host.strip().lower()
    if any(char.isspace() for char in host) or any(
        marker in host for marker in ("://", "/", "@", "?", "#")
    ):
        return frozenset()
    expected_host = _host_of(config.llm_endpoint)
    expected_protocol = (
        "chat_completions" if is_chat_completions_endpoint(config.llm_endpoint) else "responses"
    )
    if (
        host != expected_host
        or protocol != expected_protocol
        or provider != config.recognized_provider()
    ):
        return frozenset()
    return frozenset({host})


def infer_trust(
    host: str,
    explicit: str | None = None,
    *,
    processor_hosts: frozenset[str] | set[str] | None = None,
) -> str:
    """Classify a provider host.

    Explicit ``SCREENSCRIBE_AGENT_PRIMARY_TRUST`` / fallback trust wins.
    Loopback is ``local``; Libraxis is ``internal``. A remaining host is
    ``processor`` only when ``analysis_hosts`` received a matching persisted
    semantic-LLM receipt. Everything else is ``external``.
    """
    if explicit:
        level = explicit.strip().lower()
        if level in _TRUST_LEVELS:
            return level
    lowered = (host or "").lower()
    if lowered in {"localhost", "127.0.0.1", "::1"}:
        return "local"
    if lowered in _INTERNAL_HOSTS or lowered.endswith(".libraxis.cloud"):
        return "internal"
    if processor_hosts and lowered in processor_hosts:
        return "processor"
    return "external"


def build_providers(
    config: ScreenScribeConfig,
    *,
    processing_provenance: dict[str, Any] | None = None,
) -> list[AgentProvider]:
    """PRIMARY = screenscribe LLM Responses endpoint; FALLBACK = optional Anthropic."""
    providers: list[AgentProvider] = []
    processor_hosts = analysis_hosts(config, processing_provenance)
    primary_key = config.get_llm_api_key()
    if primary_key:
        url = config.llm_endpoint
        host = _host_of(url)
        providers.append(
            AgentProvider(
                name="primary",
                protocol=("chat_completions" if is_chat_completions_endpoint(url) else "responses"),
                key=primary_key,
                model=config.llm_model,
                url=url,
                trust=infer_trust(
                    host, config.agent_primary_trust, processor_hosts=processor_hosts
                ),
                slot="primary",
                host=host,
            )
        )

    fallback_key = (
        os.environ.get("SCREENSCRIBE_AGENT_FALLBACK_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    if fallback_key:
        protocol = (
            (os.environ.get("SCREENSCRIBE_AGENT_FALLBACK_PROTOCOL") or "anthropic").strip().lower()
        )
        url = (
            os.environ.get("SCREENSCRIBE_AGENT_FALLBACK_URL")
            or "https://api.anthropic.com/v1/messages"
        ).strip()
        host = _host_of(url)
        trust_override = os.environ.get("SCREENSCRIBE_AGENT_FALLBACK_TRUST")
        model = (os.environ.get("SCREENSCRIBE_AGENT_FALLBACK_MODEL") or "claude-opus-4-6").strip()
        resolved_protocol = "anthropic"
        if protocol != "anthropic":
            resolved_protocol = (
                "chat_completions" if is_chat_completions_endpoint(url) else "responses"
            )
        providers.append(
            AgentProvider(
                name="fallback",
                protocol=resolved_protocol,
                key=fallback_key,
                model=model,
                url=url,
                trust=infer_trust(host, trust_override, processor_hosts=processor_hosts),
                slot="fallback",
                host=host,
            )
        )
    return providers


def apply_egress(
    providers: list[AgentProvider], egress: str
) -> tuple[list[AgentProvider], list[AgentProvider]]:
    """Keep local/internal/processor always; keep external only when egress is allow."""
    policy = normalize_agent_egress(egress)
    kept: list[AgentProvider] = []
    skipped: list[AgentProvider] = []
    for provider in providers:
        if provider.trust in _KEPT_UNDER_DENY or policy == "allow":
            kept.append(provider)
        else:
            skipped.append(provider)
    return kept, skipped


async def stream_agent_chat(
    *,
    config: ScreenScribeConfig,
    report: dict[str, Any],
    tools: ReportToolbelt,
    message: str,
    history: list[dict[str, str]] | None = None,
    previous_response_id: str | None = None,
    previous_response_provider: str | None = None,
    previous_response_protocol: str | None = None,
    previous_response_host: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for one user turn."""
    provenance = report.get("processing_provenance")
    providers, skipped = apply_egress(
        build_providers(
            config,
            processing_provenance=provenance if isinstance(provenance, dict) else None,
        ),
        config.agent_egress,
    )
    if not providers:
        yield format_sse("error", {"message": _no_provider_message(skipped, config)})
        return
    bound_previous_id = _bound_previous_response_id(
        previous_response_id,
        provider_name=previous_response_provider,
        protocol=previous_response_protocol,
        host=previous_response_host,
        providers=providers,
    )
    turn = prepare_turn(
        report=report,
        message=message,
        history=history,
        previous_response_id=bound_previous_id,
    )

    last_error: str | None = None
    for provider in providers:
        emitted = False
        try:
            async for frame in _run_provider(
                config=config,
                provider=provider,
                turn=turn,
                tools=tools,
            ):
                emitted = True
                yield frame
            return
        except Exception as exc:
            last_error = redact_error_message(exc)
            logger.warning("Agent provider %s failed: %s", provider.name, last_error)
            if emitted:
                # Retrying a whole turn after any visible token/tool frame would
                # splice two providers' answers into one SSE response.
                yield format_sse("error", {"message": last_error})
                return

    yield format_sse(
        "error",
        {"message": last_error or "All agent providers failed."},
    )


async def collect_agent_chat(
    *,
    config: ScreenScribeConfig,
    report: dict[str, Any],
    tools: ReportToolbelt,
    message: str,
    history: list[dict[str, str]] | None = None,
    previous_response_id: str | None = None,
    previous_response_provider: str | None = None,
    previous_response_protocol: str | None = None,
    previous_response_host: str | None = None,
) -> dict[str, Any]:
    """Non-streaming turn: concatenate token text and return the chain id."""
    text_parts: list[str] = []
    response_id: str | None = None
    error: str | None = None
    async for frame in stream_agent_chat(
        config=config,
        report=report,
        tools=tools,
        message=message,
        history=history,
        previous_response_id=previous_response_id,
        previous_response_provider=previous_response_provider,
        previous_response_protocol=previous_response_protocol,
        previous_response_host=previous_response_host,
    ):
        event, payload = _parse_sse_frame(frame)
        if event == "token":
            piece = payload.get("text")
            if isinstance(piece, str):
                text_parts.append(piece)
        elif event == "done":
            rid = payload.get("response_id")
            response_id = rid if isinstance(rid, str) else None
        elif event == "error":
            msg = payload.get("message")
            error = msg if isinstance(msg, str) else "Agent error."
    if error:
        raise AgentChatError(error)
    return {"text": "".join(text_parts), "response_id": response_id}


def _bound_previous_response_id(
    response_id: str | None,
    *,
    provider_name: str | None,
    protocol: str | None,
    host: str | None,
    providers: list[AgentProvider],
) -> str | None:
    """Accept a cursor only with the exact primary Responses identity that minted it."""
    if not response_id or not provider_name or not protocol or not host:
        return None
    for provider in providers:
        if (
            provider.slot == "primary"
            and _supports_stateful_response_chain(provider)
            and provider.name == provider_name
            and provider.protocol == protocol
            and provider.host == host.strip().lower()
        ):
            return response_id
    return None


def _supports_stateful_response_chain(provider: AgentProvider) -> bool:
    """Whether top-level instructions may accompany a response cursor.

    xAI currently rejects ``instructions`` together with
    ``previous_response_id``. The review agent must resend its trusted policy and
    report seed, so xAI uses the stateless full-history path instead.
    """
    return provider.protocol == "responses" and provider.host != "api.x.ai"


async def _run_provider(
    *,
    config: ScreenScribeConfig,
    provider: AgentProvider,
    turn: PreparedTurn,
    tools: ReportToolbelt,
) -> AsyncIterator[str]:
    include_repo = tools.repo_root is not None
    current_input: list[dict[str, Any]] = list(turn.input_items)
    stateful_chain = _supports_stateful_response_chain(provider)
    # Chain ids are scoped to the provider endpoint that minted them. The client
    # cursor belongs to the primary; a fallback starts from seed/history and may
    # chain only ids minted by its own later tool rounds.
    previous = turn.previous_response_id if stateful_chain and provider.slot == "primary" else None
    last_response_id = previous

    for _round in range(_MAX_TOOL_ROUNDS):
        if provider.protocol == "chat_completions":
            raise AgentChatError(
                "Review agent requires a Responses API endpoint; the configured "
                "LLM endpoint uses /v1/chat/completions."
            )
        if provider.protocol == "anthropic":
            payload = {
                "model": provider.model,
                "instructions": turn.instructions,
                "input": current_input,
                "tools": anthropic_tool_schemas(include_repo=include_repo),
            }
        else:
            payload = _build_responses_payload(
                config=config,
                provider=provider,
                # Responses does not carry top-level instructions across a
                # previous_response_id chain; resend the stable policy every round.
                instructions=turn.instructions,
                input_items=current_input,
                previous_response_id=previous,
                include_repo=include_repo,
            )
        round_result = await _dispatch_round(provider, payload)
        if round_result.error:
            raise AgentChatError(round_result.error)
        if round_result.response_id and stateful_chain:
            last_response_id = round_result.response_id
            previous = round_result.response_id
        if round_result.text:
            yield format_sse("token", {"text": round_result.text})
        if not round_result.function_calls:
            yield format_sse(
                "done",
                {
                    "response_id": last_response_id,
                    "provider": provider.name,
                    "protocol": provider.protocol,
                    "host": provider.host,
                },
            )
            return

        outputs: list[dict[str, Any]] = []
        anthropic_results: list[dict[str, Any]] = []
        for call in round_result.function_calls:
            yield format_sse("tool_call", {"name": call.name, "input": call.arguments})
            result_json = tools.execute(call.name, call.arguments)
            try:
                result_obj: Any = json.loads(result_json)
            except json.JSONDecodeError:
                result_obj = result_json
            yield format_sse("tool_result", {"name": call.name, "result": result_obj})
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result_json,
                }
            )
            anthropic_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.call_id,
                    "content": result_json,
                }
            )
        if provider.protocol == "anthropic":
            tool_uses = round_result.tool_use_blocks or [
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in round_result.function_calls
            ]
            current_input = [
                *current_input,
                {"role": "assistant", "content": tool_uses},
                {"role": "user", "content": anthropic_results},
            ]
        elif stateful_chain:
            current_input = outputs
        else:
            # xAI rejects top-level instructions together with a cursor. Keep the
            # trusted instructions and run statelessly: resend the full user/chat
            # context, then append the assistant tool call(s) and their outputs in
            # the Responses API input-item shape.
            response_items = [dict(item) for item in round_result.response_output_items]
            if not response_items:
                if round_result.text:
                    response_items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": round_result.text}],
                        }
                    )
                response_items.extend(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }
                    for call in round_result.function_calls
                )
            current_input = [*current_input, *response_items, *outputs]

    yield format_sse(
        "error",
        {"message": f"Exceeded {_MAX_TOOL_ROUNDS} tool-call rounds."},
    )


def _build_responses_payload(
    *,
    config: ScreenScribeConfig,
    provider: AgentProvider,
    instructions: str | None,
    input_items: list[dict[str, Any]],
    previous_response_id: str | None,
    include_repo: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": provider.model,
        "input": input_items,
        "tools": responses_tool_schemas(include_repo=include_repo),
        "stream": True,
    }
    if instructions:
        payload["instructions"] = instructions
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    elif provider.host == "api.x.ai":
        # xAI's stateless tool-loop contract replays ``response.output``. Ask
        # explicitly for encrypted reasoning so Grok 4.6 returns the reasoning
        # items needed to preserve agentic state across those full-history calls.
        payload["include"] = ["reasoning.encrypted_content"]
    reasoning = responses_reasoning_options(provider.url, config.get_llm_reasoning_effort())
    if reasoning:
        payload["reasoning"] = reasoning
    return payload


async def _dispatch_round(provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
    if round_tripper is not None:
        return await round_tripper(provider, payload)
    if provider.protocol == "anthropic":
        return await _anthropic_round(provider, payload)
    return await _responses_round(provider, payload)


async def _responses_round(provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
    headers = {
        "Authorization": f"Bearer {provider.key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    text_parts: list[str] = []
    final_text: str | None = None
    response_id: str | None = None
    calls: dict[str, dict[str, Any]] = {}
    item_to_call: dict[str, str] = {}
    response_output_items: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=90.0) as client:
        async with client.stream("POST", provider.url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise AgentChatError(
                    f"Provider {provider.name} HTTP {resp.status_code}: "
                    f"{redact_error_message(Exception(body[:400]))}"
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                stream_error = extract_stream_error_event(event)
                if stream_error is not None:
                    raise AgentChatError(str(stream_error))
                event_type = str(event.get("type") or "")
                if event_type == "response.created":
                    created = event.get("response")
                    if isinstance(created, dict):
                        response_id = created.get("id") or response_id
                elif event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
                elif event_type == "response.output_text.done":
                    text = event.get("text")
                    if isinstance(text, str):
                        final_text = text
                elif event_type in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                    "response.output_item.added",
                    "response.output_item.done",
                    "response.completed",
                    "response.done",
                }:
                    _ingest_function_events(event, calls, item_to_call)
                    item = event.get("item")
                    if event_type == "response.output_item.done" and isinstance(item, dict):
                        _upsert_response_output_item(response_output_items, item)
                    completed = event.get("response")
                    if isinstance(completed, dict) and completed.get("id"):
                        response_id = completed.get("id") or response_id
                        output = completed.get("output")
                        if isinstance(output, list):
                            _ingest_output_list(output, calls, item_to_call)
                            response_output_items = [
                                dict(item) for item in output if isinstance(item, dict)
                            ]
    return ProviderRound(
        text="".join(text_parts) if text_parts else (final_text or ""),
        response_id=response_id if isinstance(response_id, str) else None,
        function_calls=_calls_from_bucket(calls),
        response_output_items=response_output_items,
    )


def _upsert_response_output_item(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    """Keep completed Responses output items in their emitted order."""
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        for index, existing in enumerate(items):
            if existing.get("id") == item_id:
                items[index] = dict(item)
                return
    items.append(dict(item))


def _ingest_function_events(
    event: dict[str, Any],
    calls: dict[str, dict[str, Any]],
    item_to_call: dict[str, str],
) -> None:
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}:
        _ingest_call_item(item, calls, item_to_call)
    name = event.get("name")
    item_id = event.get("item_id")
    event_call_id = event.get("call_id") or event.get("id")
    key = item_to_call.get(str(item_id or "")) or str(event_call_id or item_id or name or "call")
    arguments = event.get("arguments")
    delta = event.get("delta")
    if name or isinstance(arguments, (str, dict)) or isinstance(delta, str):
        bucket = calls.setdefault(key, {"name": "", "call_id": key, "arguments": ""})
        if isinstance(name, str) and name:
            bucket["name"] = name
        if isinstance(event_call_id, str) and event_call_id:
            bucket["call_id"] = event_call_id
        if isinstance(arguments, str):
            bucket["arguments"] = arguments
        elif isinstance(arguments, dict):
            bucket["arguments"] = json.dumps(arguments)
        elif isinstance(delta, str):
            bucket["arguments"] = str(bucket.get("arguments") or "") + delta


def _ingest_output_list(
    output: list[Any],
    calls: dict[str, dict[str, Any]],
    item_to_call: dict[str, str],
) -> None:
    for item in output:
        if isinstance(item, dict):
            _ingest_call_item(item, calls, item_to_call)


def _ingest_call_item(
    item: dict[str, Any],
    calls: dict[str, dict[str, Any]],
    item_to_call: dict[str, str],
) -> None:
    if item.get("type") not in {"function_call", "tool_call"}:
        return
    item_id = str(item.get("id") or "")
    call_id = str(item.get("call_id") or item_id)
    name = str(item.get("name") or "")
    if not call_id and not name:
        return
    key = call_id or name
    if item_id:
        item_to_call[item_id] = key
    bucket = calls.setdefault(key, {"name": name, "call_id": call_id or key, "arguments": ""})
    if name:
        bucket["name"] = name
    if call_id:
        bucket["call_id"] = call_id
    arguments = item.get("arguments")
    if isinstance(arguments, str) and arguments:
        bucket["arguments"] = arguments
    elif isinstance(arguments, dict):
        bucket["arguments"] = json.dumps(arguments)


def _calls_from_bucket(calls: dict[str, dict[str, Any]]) -> list[FunctionCall]:
    parsed: list[FunctionCall] = []
    for bucket in calls.values():
        name = str(bucket.get("name") or "")
        if not name:
            continue
        raw_args = bucket.get("arguments") or "{}"
        if isinstance(raw_args, dict):
            arguments = raw_args
        else:
            try:
                loaded = json.loads(str(raw_args))
            except json.JSONDecodeError:
                loaded = {}
            arguments = loaded if isinstance(loaded, dict) else {}
        parsed.append(
            FunctionCall(
                name=name,
                call_id=str(bucket.get("call_id") or name),
                arguments=arguments,
            )
        )
    return parsed


async def _anthropic_round(provider: AgentProvider, payload: dict[str, Any]) -> ProviderRound:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise AgentChatError(
            "Anthropic fallback requested but the optional extra is not installed "
            "(pip install 'screenscribe[anthropic]')."
        ) from exc

    client = AsyncAnthropic(api_key=provider.key, base_url=_anthropic_base_url(provider.url))
    messages = _anthropic_messages_from_input(payload.get("input") or [])
    system = str(payload.get("instructions") or "")
    tools = payload.get("tools") or []
    kwargs: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": 4096,
        "messages": messages,
        "tools": tools,
    }
    if system:
        kwargs["system"] = system

    text_parts: list[str] = []
    async with client.messages.stream(**kwargs) as stream:
        async for event in stream:
            if getattr(event, "type", None) != "content_block_delta":
                continue
            delta = getattr(event, "delta", None)
            if getattr(delta, "type", None) == "text_delta":
                piece = getattr(delta, "text", "")
                if piece:
                    text_parts.append(piece)
        final = await stream.get_final_message()

    calls: list[FunctionCall] = []
    tool_use_blocks: list[dict[str, Any]] = []
    if getattr(final, "stop_reason", None) == "tool_use":
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            raw_input = getattr(block, "input", {}) or {}
            block_id = str(getattr(block, "id", "") or "")
            block_name = str(getattr(block, "name", "") or "")
            normalized_input = raw_input if isinstance(raw_input, dict) else {}
            tool_use_blocks.append(
                {
                    "type": "tool_use",
                    "id": block_id,
                    "name": block_name,
                    "input": normalized_input,
                }
            )
            calls.append(
                FunctionCall(
                    name=block_name,
                    call_id=block_id,
                    arguments=normalized_input,
                )
            )
    return ProviderRound(
        text="".join(text_parts),
        # Anthropic message ids cannot be replayed as Responses chain ids.
        response_id=None,
        function_calls=calls,
        tool_use_blocks=tool_use_blocks,
    )


def _anthropic_messages_from_input(items: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": item.get("call_id"),
                    "content": item.get("output") or "",
                }
            )
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content")
        blocks: list[dict[str, Any]] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "input_text":
                    text = str(block.get("text") or "")
                    if text:
                        blocks.append({"type": "text", "text": text})
                elif isinstance(block, dict) and block.get("type") in {
                    "text",
                    "tool_result",
                    "tool_use",
                }:
                    blocks.append(dict(block))
        elif isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        if blocks:
            messages.append({"role": role, "content": blocks})
    if tool_results:
        messages.append({"role": "user", "content": tool_results})
    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages


def _anthropic_base_url(messages_url: str) -> str:
    """Convert a configured Messages endpoint to the SDK's base URL."""
    try:
        parts = urlsplit(messages_url)
    except ValueError:
        return messages_url
    path = parts.path.rstrip("/")
    for suffix in ("/v1/messages", "/messages"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _no_provider_message(skipped: list[AgentProvider], config: ScreenScribeConfig) -> str:
    if skipped:
        names = ", ".join(f"{p.name} ({p.host}, trust={p.trust})" for p in skipped)
        return (
            "All agent providers were skipped by egress policy "
            f"(SCREENSCRIBE_AGENT_EGRESS={normalize_agent_egress(config.agent_egress)}). "
            f"Skipped: {names}. A host is kept as trust=processor only when the "
            "report carries a matching successful semantic-LLM receipt. Set "
            "SCREENSCRIBE_AGENT_EGRESS=allow to permit other external providers. "
            "SCREENSCRIBE_AGENT_PRIMARY_TRUST=external opts the analysis host "
            "out of that keep-list."
        )
    return "No LLM API key configured for the review agent."


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _parse_sse_frame(frame: str) -> tuple[str, dict[str, Any]]:
    event = ""
    data = ""
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
    payload: dict[str, Any]
    try:
        loaded = json.loads(data) if data else {}
        payload = loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        payload = {}
    return event, payload
