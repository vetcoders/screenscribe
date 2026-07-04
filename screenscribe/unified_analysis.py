"""Unified VLM-powered analysis combining semantic and vision analysis.

This module replaces the separate semantic.py + vision.py pipeline with a single
VLM call that analyzes both the screenshot AND full transcript context together.

Benefits:
- Single API call instead of two (LLM + VLM)
- VLM sees both image and full context simultaneously
- Better understanding of user intent by combining visual and verbal cues
- Reduced latency and API costs
- Parallel processing with staggered starts for better throughput
"""

from __future__ import annotations

# Re-exported so monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", ...)
# keeps resolving after the transport/summary code moved into the unified package.
import httpx as httpx

from .unified._console import console as console
from .unified.analyze_one import (
    analyze_finding_unified as analyze_finding_unified,
)
from .unified.analyze_one import (
    analyze_finding_unified_streaming as analyze_finding_unified_streaming,
)
from .unified.dedup import (
    deduplicate_findings as deduplicate_findings,
)
from .unified.finding import UnifiedFinding as UnifiedFinding
from .unified.llm_merge import (
    llm_merge_findings as llm_merge_findings,
)
from .unified.orchestrator import (
    MAX_UNIFIED_FAILURE_RATIO as MAX_UNIFIED_FAILURE_RATIO,
)
from .unified.orchestrator import (
    MAX_WORKERS as MAX_WORKERS,
)
from .unified.orchestrator import (
    STAGGER_DELAY as STAGGER_DELAY,
)
from .unified.orchestrator import (
    _TaskState as _TaskState,
)
from .unified.orchestrator import (
    analyze_all_findings_unified as analyze_all_findings_unified,
)
from .unified.response_parsing import (
    _clean_summary_response as _clean_summary_response,
)
from .unified.response_parsing import (
    _normalize_string_list as _normalize_string_list,
)
from .unified.response_parsing import (
    _normalize_unified_response_data as _normalize_unified_response_data,
)
from .unified.response_parsing import (
    extract_response_content as extract_response_content,
)
from .unified.response_parsing import (
    parse_json_response as parse_json_response,
)
from .unified.summaries import (
    _build_local_executive_summary as _build_local_executive_summary,
)
from .unified.summaries import (
    generate_unified_summary as generate_unified_summary,
)
from .unified.summaries import (
    generate_visual_summary_unified as generate_visual_summary_unified,
)
from .unified.wire import (
    _extract_stream_delta as _extract_stream_delta,
)

__all__ = [
    "MAX_UNIFIED_FAILURE_RATIO",
    "MAX_WORKERS",
    "STAGGER_DELAY",
    "UnifiedFinding",
    "analyze_all_findings_unified",
    "analyze_finding_unified",
    "analyze_finding_unified_streaming",
    "console",
    "deduplicate_findings",
    "extract_response_content",
    "generate_unified_summary",
    "generate_visual_summary_unified",
    "httpx",
    "llm_merge_findings",
    "parse_json_response",
]
