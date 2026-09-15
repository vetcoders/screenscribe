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
    event: done         data: {"response_id": ...}
    event: error        data: {"message": ...}

``tool_result.result`` may be a ``review_patch`` or ``review_plan`` object from
the write tools. The SSE envelope is unchanged; the browser applies the patch.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from ..api_utils import (
    extract_stream_error_event,
    redact_error_message,
    responses_reasoning_options,
)
from ..config import ScreenScribeConfig
from .context import PreparedTurn, prepare_turn
from .tools import ReportToolbelt, anthropic_tool_schemas, responses_tool_schemas

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 8
_INTERNAL_HOSTS = {"api.libraxis.cloud"}
_TRUST_LEVELS = {"local", "internal", "processor", "external"}
_KEPT_UNDER_DENY = {"local", "internal", "processor"}

# Tests replace this to avoid the network.
RoundTripper = Callable[["AgentProvider", dict[str, Any]], Awaitable["ProviderRound"]]
round_tripper: RoundTripper | None = None


class AgentChatError(Exception):
    """Agent turn failed after every provider (or before any could run)."""


class AgentChatRequest(BaseModel):
    """POST /api/agent/chat and /api/agent/chat/stream body."""

    message: str = Field(..., min_length=1)
    history: list[dict[str, str]] = Field(default_factory=list)
    previous_response_id: str | None = None


@dataclass(frozen=True)
class AgentProvider:
    name: str
    protocol: str  # "responses" | "anthropic"
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


def format_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def normalize_agent_egress(value: str | None) -> str:
    raw = (value or "deny").strip().lower()
    return "allow" if raw == "allow" else "deny"


def analysis_hosts(config: ScreenScribeConfig) -> frozenset[str]:
    """Hosts that already received this recording (STT, LLM, vision)."""
    hosts: set[str] = set()
    for url in (config.stt_endpoint, config.llm_endpoint, config.vision_endpoint):
        host = _host_of(url)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def infer_trust(
    host: str,
    explicit: str | None = None,
    *,
    processor_hosts: frozenset[str] | set[str] | None = None,
) -> str:
    """Classify a provider host.

    Explicit ``SCREENSCRIBE_AGENT_PRIMARY_TRUST`` / fallback trust wins.
    Loopback is ``local``; Libraxis is ``internal``. A remaining host that
    already analyzed this recording (STT/LLM/vision) is ``processor``.
    Everything else is ``external``.
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


def build_providers(config: ScreenScribeConfig) -> list[AgentProvider]:
    """PRIMARY = screenscribe LLM Responses endpoint; FALLBACK = optional Anthropic."""
    providers: list[AgentProvider] = []
    processor_hosts = analysis_hosts(config)
    primary_key = config.get_llm_api_key()
    if primary_key:
        url = config.llm_endpoint
        host = _host_of(url)
        providers.append(
            AgentProvider(
                name="primary",
                protocol="responses",
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
        providers.append(
            AgentProvider(
                name="fallback",
                protocol="anthropic" if protocol == "anthropic" else "responses",
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
) -> AsyncIterator[str]:
    """Yield SSE frames for one user turn."""
    turn = prepare_turn(
        report=report,
        message=message,
        history=history,
        previous_response_id=previous_response_id,
    )
    providers, skipped = apply_egress(build_providers(config), config.agent_egress)
    if not providers:
        yield format_sse("error", {"message": _no_provider_message(skipped, config)})
        return

    last_error: str | None = None
    for provider in providers:
        try:
            async for frame in _run_provider(
                config=config,
                provider=provider,
                turn=turn,
                tools=tools,
            ):
                yield frame
            return
        except Exception as exc:
            last_error = redact_error_message(exc)
            logger.warning("Agent provider %s failed: %s", provider.name, last_error)

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


async def _run_provider(
    *,
    config: ScreenScribeConfig,
    provider: AgentProvider,
    turn: PreparedTurn,
    tools: ReportToolbelt,
) -> AsyncIterator[str]:
    include_repo = tools.repo_root is not None
    current_input: list[dict[str, Any]] = list(turn.input_items)
    previous = turn.previous_response_id
    send_instructions = previous is None
    last_response_id = previous

    for _round in range(_MAX_TOOL_ROUNDS):
        if provider.protocol == "anthropic":
            payload = {
                "model": provider.model,
                "instructions": turn.instructions if send_instructions else "",
                "input": current_input,
                "tools": anthropic_tool_schemas(include_repo=include_repo),
                "previous_response_id": previous,
            }
        else:
            payload = _build_responses_payload(
                config=config,
                provider=provider,
                instructions=turn.instructions if send_instructions else None,
                input_items=current_input,
                previous_response_id=previous,
                include_repo=include_repo,
            )
        round_result = await _dispatch_round(provider, payload)
        if round_result.error:
            raise AgentChatError(round_result.error)
        if round_result.response_id:
            last_response_id = round_result.response_id
            previous = round_result.response_id
        if round_result.text:
            yield format_sse("token", {"text": round_result.text})
        if not round_result.function_calls:
            yield format_sse("done", {"response_id": last_response_id})
            return

        outputs: list[dict[str, Any]] = []
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
        current_input = outputs
        send_instructions = False

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
    response_id: str | None = None
    calls: dict[str, dict[str, Any]] = {}
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
                    raise AgentChatError(stream_error.message)
                event_type = str(event.get("type") or "")
                if event_type == "response.created":
                    created = event.get("response")
                    if isinstance(created, dict):
                        response_id = created.get("id") or response_id
                elif event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
                elif event_type in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                    "response.output_item.added",
                    "response.output_item.done",
                    "response.completed",
                    "response.done",
                }:
                    _ingest_function_events(event, calls)
                    completed = event.get("response")
                    if isinstance(completed, dict) and completed.get("id"):
                        response_id = completed.get("id") or response_id
                        output = completed.get("output")
                        if isinstance(output, list):
                            _ingest_output_list(output, calls)
    return ProviderRound(
        text="".join(text_parts),
        response_id=response_id if isinstance(response_id, str) else None,
        function_calls=_calls_from_bucket(calls),
    )


def _ingest_function_events(event: dict[str, Any], calls: dict[str, dict[str, Any]]) -> None:
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}:
        _ingest_call_item(item, calls)
    name = event.get("name")
    call_id = event.get("item_id") or event.get("call_id") or event.get("id")
    arguments = event.get("arguments")
    if name or arguments:
        key = str(call_id or name or "call")
        bucket = calls.setdefault(key, {"name": "", "call_id": key, "arguments": ""})
        if isinstance(name, str) and name:
            bucket["name"] = name
        if isinstance(call_id, str) and call_id:
            bucket["call_id"] = call_id
        if isinstance(arguments, str):
            bucket["arguments"] = arguments
        elif event.get("type", "").endswith(".delta"):
            delta = event.get("delta")
            if isinstance(delta, str):
                bucket["arguments"] = str(bucket.get("arguments") or "") + delta


def _ingest_output_list(output: list[Any], calls: dict[str, dict[str, Any]]) -> None:
    for item in output:
        if isinstance(item, dict):
            _ingest_call_item(item, calls)


def _ingest_call_item(item: dict[str, Any], calls: dict[str, dict[str, Any]]) -> None:
    if item.get("type") not in {"function_call", "tool_call"}:
        return
    call_id = str(item.get("call_id") or item.get("id") or "")
    name = str(item.get("name") or "")
    if not call_id and not name:
        return
    key = call_id or name
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

    client = AsyncAnthropic(api_key=provider.key)
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
    if getattr(final, "stop_reason", None) == "tool_use":
        for block in final.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            raw_input = getattr(block, "input", {}) or {}
            calls.append(
                FunctionCall(
                    name=str(getattr(block, "name", "") or ""),
                    call_id=str(getattr(block, "id", "") or ""),
                    arguments=raw_input if isinstance(raw_input, dict) else {},
                )
            )
    return ProviderRound(
        text="".join(text_parts),
        response_id=getattr(final, "id", None),
        function_calls=calls,
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
        text = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "input_text":
                    text += str(block.get("text") or "")
        elif isinstance(content, str):
            text = content
        if text:
            messages.append({"role": role, "content": text})
    if tool_results:
        messages.append({"role": "user", "content": tool_results})
    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages


def _no_provider_message(skipped: list[AgentProvider], config: ScreenScribeConfig) -> str:
    if skipped:
        names = ", ".join(f"{p.name} ({p.host}, trust={p.trust})" for p in skipped)
        return (
            "All agent providers were skipped by egress policy "
            f"(SCREENSCRIBE_AGENT_EGRESS={normalize_agent_egress(config.agent_egress)}). "
            f"Skipped: {names}. Hosts already used for STT, LLM, or vision "
            "analysis are kept as trust=processor. Set "
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
