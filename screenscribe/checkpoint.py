"""Checkpoint system for resumable pipeline processing."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from .detect import Detection
from .transcribe import Segment, TranscriptionResult

console = Console()

CHECKPOINT_DIR_NAME = ".screenscribe_cache"
CHECKPOINT_FILE_NAME = "checkpoint.json"

# Bumped when the on-disk checkpoint shape changes incompatibly.
# v2 dropped the legacy per-detection semantic serialization path; checkpoints written
# by an older screenscribe (no schema marker, or a lower version) are no longer
# loadable and are skipped gracefully rather than raising on load.
CHECKPOINT_SCHEMA_VERSION = 2


@dataclass
class PipelineCheckpoint:
    """Checkpoint state for the video processing pipeline."""

    video_path: str
    video_hash: str
    output_dir: str
    language: str

    # On-disk schema marker. Defaults to the legacy sentinel (1) so that
    # checkpoints written before the marker existed deserialize as "old shape".
    schema_version: int = 1

    # Completed stages
    completed_stages: list[str] = field(default_factory=list)

    # Stage data
    transcription: dict[str, Any] | None = None
    detections: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    # Unified findings (new pipeline)
    unified_findings: list[dict[str, Any]] = field(default_factory=list)
    # Legacy fields (kept for backwards compatibility with old checkpoints)
    semantic_analyses: list[dict[str, Any]] = field(default_factory=list)
    vision_analyses: list[dict[str, Any]] = field(default_factory=list)
    executive_summary: str = ""
    visual_summary: str = ""

    def is_stage_complete(self, stage: str) -> bool:
        """Check if a stage has been completed."""
        return stage in self.completed_stages

    def mark_stage_complete(self, stage: str) -> None:
        """Mark a stage as completed."""
        if stage not in self.completed_stages:
            self.completed_stages.append(stage)

    def get_next_stage(self) -> str | None:
        """Get the next stage to process."""
        all_stages = [
            "audio",
            "transcription",
            "detection",
            "screenshots",
            "unified_analysis",  # New unified stage (replaces semantic + vision)
            "report",
        ]
        for stage in all_stages:
            if stage not in self.completed_stages:
                return stage
        return None


def get_checkpoint_dir(output_dir: Path) -> Path:
    """Get the checkpoint directory path."""
    return output_dir / CHECKPOINT_DIR_NAME


def get_checkpoint_path(output_dir: Path) -> Path:
    """Get the checkpoint file path."""
    return get_checkpoint_dir(output_dir) / CHECKPOINT_FILE_NAME


def compute_file_hash(file_path: Path, chunk_size: int = 65536) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            sha256.update(chunk)
    return sha256.hexdigest()[:16]  # First 16 chars is enough for our purposes


def save_checkpoint(checkpoint: PipelineCheckpoint, output_dir: Path) -> None:
    """Save checkpoint to disk."""
    checkpoint_dir = get_checkpoint_dir(output_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    checkpoint_path = get_checkpoint_path(output_dir)
    # Atomic write: serialize to a temp file in the SAME directory, fsync it,
    # then os.replace() onto the final path. os.replace is atomic within one
    # filesystem on both POSIX and Windows, so a crash/OOM-kill/Ctrl-C/full-disk
    # mid-write can never leave a half-written checkpoint.json — the target is
    # always either the previous good version or the new complete one.
    tmp_path = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(checkpoint), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, checkpoint_path)
    except BaseException:
        # On any failure (including KeyboardInterrupt), clean up the partial
        # temp file so it never shadows or clutters the cache dir. The final
        # checkpoint.json is untouched because os.replace did not run.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    console.print(f"[dim]Checkpoint saved: {checkpoint_path}[/]")


def load_checkpoint(output_dir: Path) -> PipelineCheckpoint | None:
    """Load checkpoint from disk if it exists."""
    checkpoint_path = get_checkpoint_path(output_dir)

    if not checkpoint_path.exists():
        return None

    try:
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"[yellow]Could not load checkpoint: {e}[/]")
        return None

    # Smart schema check: a checkpoint written by an older screenscribe either
    # lacks the version marker (defaults to 1) or carries a lower version. Such
    # checkpoints used the dropped legacy per-detection semantic path and are no longer
    # safely loadable. Warn and skip (process from scratch) instead of raising.
    if not isinstance(data, dict):
        console.print("[yellow]Checkpoint has unexpected shape; ignoring it[/]")
        return None

    found_version = data.get("schema_version", 1)
    if found_version < CHECKPOINT_SCHEMA_VERSION:
        console.print(
            f"[yellow]Checkpoint schema is outdated "
            f"(found v{found_version}, expected v{CHECKPOINT_SCHEMA_VERSION}); "
            f"skipping it and reprocessing from scratch[/]"
        )
        return None

    try:
        return PipelineCheckpoint(**data)
    except (TypeError, KeyError) as e:
        console.print(f"[yellow]Could not load checkpoint: {e}[/]")
        return None


def delete_checkpoint(output_dir: Path) -> None:
    """Delete checkpoint after successful completion."""
    checkpoint_path = get_checkpoint_path(output_dir)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        console.print("[dim]Checkpoint cleaned up[/]")

    # Also try to remove the cache directory if empty
    checkpoint_dir = get_checkpoint_dir(output_dir)
    if checkpoint_dir.exists() and not any(checkpoint_dir.iterdir()):
        checkpoint_dir.rmdir()


def checkpoint_valid_for_video(
    checkpoint: PipelineCheckpoint, video_path: Path, output_dir: Path, language: str
) -> bool:
    """Check if a checkpoint is valid for the given video."""
    # Check paths match
    if checkpoint.video_path != str(video_path.absolute()):
        console.print("[yellow]Checkpoint is for a different video file[/]")
        return False

    if checkpoint.output_dir != str(output_dir.absolute()):
        console.print("[yellow]Checkpoint is for a different output directory[/]")
        return False

    if checkpoint.language != language:
        console.print("[yellow]Checkpoint is for a different language setting[/]")
        return False

    # Check video hasn't changed
    current_hash = compute_file_hash(video_path)
    if checkpoint.video_hash != current_hash:
        console.print("[yellow]Video file has changed since checkpoint[/]")
        return False

    return True


def create_checkpoint(video_path: Path, output_dir: Path, language: str) -> PipelineCheckpoint:
    """Create a new checkpoint for a video."""
    return PipelineCheckpoint(
        video_path=str(video_path.absolute()),
        video_hash=compute_file_hash(video_path),
        output_dir=str(output_dir.absolute()),
        language=language,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
    )


# --- Serialization helpers ---


def serialize_transcription(transcription: TranscriptionResult) -> dict[str, Any]:
    """Serialize TranscriptionResult to dict."""
    return {
        "text": transcription.text,
        "language": transcription.language,
        # Persist the STT response_id: it chains the transcript into the LLM
        # semantic prefilter (review_pipeline reads transcription.response_id).
        # Without it, --resume would start a fresh, uncoupled LLM conversation.
        "response_id": transcription.response_id,
        # Synthetic-timestamp flag must survive resume so the coverage guard
        # stays disabled for transcripts that never had real STT timing (P3-4).
        "timestamps_are_synthetic": transcription.timestamps_are_synthetic,
        # Persist so --resume keeps reporting a no-speech recording as a clean
        # empty result rather than a corrupt-audio error (FW-09).
        "hallucinations_filtered": transcription.hallucinations_filtered,
        "segments": [
            {
                "id": s.id,
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "no_speech_prob": s.no_speech_prob,
                "avg_logprob": s.avg_logprob,
                "compression_ratio": s.compression_ratio,
            }
            for s in transcription.segments
        ],
    }


def deserialize_transcription(data: dict[str, Any]) -> TranscriptionResult:
    """Deserialize dict to TranscriptionResult."""
    segments = [
        Segment(
            id=s["id"],
            start=s["start"],
            end=s["end"],
            text=s["text"],
            no_speech_prob=s.get("no_speech_prob", 0.0),  # Default for old checkpoints
            avg_logprob=s.get("avg_logprob", 0.0),
            compression_ratio=s.get("compression_ratio", 0.0),
        )
        for s in data["segments"]
    ]
    return TranscriptionResult(
        text=data["text"],
        language=data["language"],
        segments=segments,
        # Default empty for old checkpoints written before response_id was persisted.
        response_id=data.get("response_id", ""),
        timestamps_are_synthetic=data.get("timestamps_are_synthetic", False),
        hallucinations_filtered=data.get("hallucinations_filtered", False),
    )


def serialize_detection(detection: Detection) -> dict[str, Any]:
    """Serialize Detection to dict."""
    return {
        "segment": {
            "id": detection.segment.id,
            "start": detection.segment.start,
            "end": detection.segment.end,
            "text": detection.segment.text,
            "no_speech_prob": detection.segment.no_speech_prob,
            "avg_logprob": detection.segment.avg_logprob,
            "compression_ratio": detection.segment.compression_ratio,
        },
        "category": detection.category,
        "keywords_found": detection.keywords_found,
        "context": detection.context,
    }


def deserialize_detection(data: dict[str, Any]) -> Detection:
    """Deserialize dict to Detection."""
    seg_data = data["segment"]
    segment = Segment(
        id=seg_data["id"],
        start=seg_data["start"],
        end=seg_data["end"],
        text=seg_data["text"],
        no_speech_prob=seg_data.get("no_speech_prob", 0.0),
        avg_logprob=seg_data.get("avg_logprob", 0.0),
        compression_ratio=seg_data.get("compression_ratio", 0.0),
    )
    return Detection(
        segment=segment,
        category=data["category"],
        keywords_found=data["keywords_found"],
        context=data["context"],
    )


def serialize_screenshot(detection: Detection, path: Path) -> dict[str, Any]:
    """Serialize screenshot info to dict."""
    return {
        "detection": serialize_detection(detection),
        "path": str(path),
    }


def deserialize_screenshot(data: dict[str, Any]) -> tuple[Detection, Path]:
    """Deserialize dict to screenshot tuple."""
    detection = deserialize_detection(data["detection"])
    path = Path(data["path"])
    return (detection, path)


# --- UnifiedFinding serialization (new unified pipeline) ---


def serialize_unified_finding(finding: Any) -> dict[str, Any]:
    """Serialize UnifiedFinding to dict.

    Args:
        finding: UnifiedFinding instance

    Returns:
        Serialized dict
    """
    return {
        "detection_id": finding.detection_id,
        "screenshot_path": str(finding.screenshot_path) if finding.screenshot_path else None,
        "timestamp": finding.timestamp,
        # Semantic fields
        "category": finding.category,
        "is_issue": finding.is_issue,
        "sentiment": finding.sentiment,
        "severity": finding.severity,
        "summary": finding.summary,
        "action_items": finding.action_items,
        "affected_components": finding.affected_components,
        "suggested_fix": finding.suggested_fix,
        # Vision fields
        "ui_elements": finding.ui_elements,
        "issues_detected": finding.issues_detected,
        "accessibility_notes": finding.accessibility_notes,
        "design_feedback": finding.design_feedback,
        "technical_observations": finding.technical_observations,
        # API tracking
        "response_id": finding.response_id,
        # Deduplication tracking
        "merged_from_ids": finding.merged_from_ids,
        # Reliability tracking
        "confidence": getattr(finding, "confidence", "high"),
        "parsed_from_unstructured_output": getattr(
            finding, "parsed_from_unstructured_output", False
        ),
    }


def deserialize_unified_finding(data: dict[str, Any]) -> Any:
    """Deserialize dict to UnifiedFinding.

    Args:
        data: Serialized dict

    Returns:
        UnifiedFinding instance
    """
    # Import here to avoid circular imports
    from .unified_analysis import UnifiedFinding

    return UnifiedFinding(
        detection_id=data["detection_id"],
        screenshot_path=Path(data["screenshot_path"]) if data.get("screenshot_path") else None,
        timestamp=data["timestamp"],
        # Semantic fields
        category=data.get("category", "unknown"),
        is_issue=data.get("is_issue", True),
        sentiment=data.get("sentiment", "problem"),
        severity=data.get("severity", "medium"),
        summary=data.get("summary", ""),
        action_items=data.get("action_items", []),
        affected_components=data.get("affected_components", []),
        suggested_fix=data.get("suggested_fix", ""),
        # Vision fields
        ui_elements=data.get("ui_elements", []),
        issues_detected=data.get("issues_detected", []),
        accessibility_notes=data.get("accessibility_notes", []),
        design_feedback=data.get("design_feedback", ""),
        technical_observations=data.get("technical_observations", ""),
        # API tracking
        response_id=data.get("response_id", ""),
        # Deduplication tracking
        merged_from_ids=[tuple(x) for x in data.get("merged_from_ids", [])],
        # Reliability tracking
        confidence=data.get("confidence", "high"),
        parsed_from_unstructured_output=data.get("parsed_from_unstructured_output", False),
    )
