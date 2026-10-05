"""Review-report agent: Responses API chat with screenscribe tools.

Ported in spirit from family-onko-portal ``ai_chat.py`` (provider routing,
Responses ``previous_response_id`` chaining, Anthropic tool loop, egress).
Medical tools are not carried over. The SSE contract is the one w1-05 binds
against, not the portal's untyped ``data: {type: ...}`` frames.
"""

from .chat import (
    AgentChatError,
    AgentChatRequest,
    AgentProvider,
    apply_egress,
    build_providers,
    collect_agent_chat,
    format_sse,
    stream_agent_chat,
)
from .context import PreparedTurn, prepare_turn, report_chain_response_id, seed_report_context
from .tools import ReportToolbelt

__all__ = [
    "AgentChatError",
    "AgentChatRequest",
    "AgentProvider",
    "PreparedTurn",
    "ReportToolbelt",
    "apply_egress",
    "build_providers",
    "collect_agent_chat",
    "format_sse",
    "prepare_turn",
    "report_chain_response_id",
    "seed_report_context",
    "stream_agent_chat",
]
