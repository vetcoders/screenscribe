"""UnifiedFinding dataclass — combined semantic + visual analysis result.

Extracted first as the import sink for the unified package: it has zero
internal dependencies, so pulling it out before the rest removes cycle risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UnifiedFinding:
    """Combined semantic + visual analysis of a finding.

    This dataclass holds the combined semantic and visual result, produced by a
    single VLM call that analyzes both the screenshot and transcript context
    together.
    """

    detection_id: int
    screenshot_path: Path | None
    timestamp: float

    # From semantic analysis
    category: str  # bug, change, ui, performance, accessibility, other
    is_issue: bool  # True if user reports a problem, False if confirms OK
    sentiment: str  # "problem", "positive", "neutral"
    severity: str  # "critical", "high", "medium", "low", "none"
    summary: str
    action_items: list[str]
    affected_components: list[str]
    suggested_fix: str

    # From vision analysis
    ui_elements: list[str]
    issues_detected: list[str]
    accessibility_notes: list[str]
    design_feedback: str
    technical_observations: str

    # API response tracking
    response_id: str = ""  # For conversation chaining between findings

    # Deduplication tracking - stores (detection_id, timestamp) of merged findings
    merged_from_ids: list[tuple[int, float]] = field(default_factory=list)

    # Reliability tracking for schema drift / raw-text fallbacks
    confidence: str = "high"  # "high" or "degraded"
    parsed_from_unstructured_output: bool = False
