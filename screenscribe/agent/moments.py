"""Shared report helpers for discoverable manual review moments."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_MANUAL_ADDRESS_PREFIX = "manual:"


def manual_marker_id(marker: dict[str, Any]) -> Any:
    """Return the persisted raw identity of a manual marker."""
    marker_id = marker.get("marker_id", marker.get("id"))
    if marker_id is None or marker_id == "":
        return None
    return marker_id


def manual_markers(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return enriched copies of manual markers from the saved report shapes.

    ``manual_review.markers`` is the durable canonical source. A saved report can
    repeat the same records under ``human_review.manual_frames`` for the UI and
    keep analysis under ``manual_review.results``. Canonical marker values win;
    compatibility sources only fill empty fields. Returned records are detached
    from ``report`` so tool reads cannot mutate the loaded report.
    """
    markers: list[dict[str, Any]] = []
    marker_indexes: dict[str, int] = {}

    sources: list[object] = []
    manual = report.get("manual_review")
    if isinstance(manual, dict):
        sources.append(manual.get("markers"))
    human = report.get("human_review")
    if isinstance(human, dict):
        sources.append(human.get("manual_frames"))

    for source in sources:
        if not isinstance(source, list):
            continue
        for marker in source:
            if not isinstance(marker, dict):
                continue
            marker_id = manual_marker_id(marker)
            if marker_id is not None:
                identity = str(marker_id)
                existing_index = marker_indexes.get(identity)
                if existing_index is not None:
                    markers[existing_index] = _fill_missing(markers[existing_index], marker)
                    continue
                marker_indexes[identity] = len(markers)
            markers.append(deepcopy(marker))

    results_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(manual, dict):
        results = manual.get("results")
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, dict):
                    continue
                marker_id = manual_marker_id(result)
                if marker_id is None:
                    continue
                identity = str(marker_id)
                existing_result = results_by_id.get(identity)
                results_by_id[identity] = (
                    _fill_missing(existing_result, result)
                    if existing_result is not None
                    else deepcopy(result)
                )

    for marker in markers:
        marker_id = manual_marker_id(marker)
        if marker_id is None:
            continue
        saved_result = results_by_id.get(str(marker_id))
        if saved_result is None:
            continue
        nested = marker.get("result")
        marker["result"] = _fill_missing(
            nested if isinstance(nested, dict) else {},
            saved_result,
        )
    return markers


def manual_marker_addresses(
    report: dict[str, Any],
) -> list[tuple[object, dict[str, Any]]]:
    """Return unambiguous addresses for the report's normalized manual moments.

    A raw marker id remains its address unless an AI finding or merged member
    already owns that string. Collisions receive a ``manual:`` prefix; the
    prefix is repeated when needed so a real marker id such as ``manual:m2``
    remains independently addressable.
    """
    markers = manual_markers(report)
    raw_ids = {
        str(marker_id) for marker in markers if (marker_id := manual_marker_id(marker)) is not None
    }
    reserved = _ai_addresses(report)
    assigned: set[str] = set()
    addressed: list[tuple[object, dict[str, Any]]] = []

    for marker in markers:
        marker_id = manual_marker_id(marker)
        if marker_id is None:
            addressed.append((None, marker))
            continue
        raw = str(marker_id)
        if raw not in reserved and raw not in assigned:
            address: object = marker_id
        else:
            candidate = f"{_MANUAL_ADDRESS_PREFIX}{raw}"
            while candidate in reserved or candidate in raw_ids or candidate in assigned:
                candidate = f"{_MANUAL_ADDRESS_PREFIX}{candidate}"
            address = candidate
        assigned.add(str(address))
        addressed.append((address, marker))
    return addressed


def manual_marker_result(marker: dict[str, Any]) -> dict[str, Any]:
    """Return the analysis nested in a normalized manual marker, if any."""
    result = marker.get("result")
    return result if isinstance(result, dict) else {}


def manual_marker_summary(marker: dict[str, Any]) -> object:
    """Choose the best compact human/model summary without inventing content."""
    result = manual_marker_result(marker)
    return (
        result.get("summary")
        or marker.get("summary")
        or marker.get("notes")
        or marker.get("transcript")
        or ""
    )


def _fill_missing(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Copy ``primary`` and fill only absent/empty values from ``fallback``."""
    merged = deepcopy(primary)
    for key, fallback_value in fallback.items():
        if not _meaningful(fallback_value):
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(fallback_value, dict):
            merged[key] = _fill_missing(current, fallback_value)
        elif not _meaningful(current):
            merged[key] = deepcopy(fallback_value)
    return merged


def _meaningful(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _ai_addresses(report: dict[str, Any]) -> set[str]:
    reserved: set[str] = set()
    findings = report.get("findings")
    if not isinstance(findings, list):
        return reserved
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding_id = finding.get("id")
        if finding_id is not None and finding_id != "":
            reserved.add(str(finding_id))
        members = finding.get("merged_frames")
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            member_id = member.get("id")
            if member_id is not None and member_id != "":
                reserved.add(str(member_id))
    return reserved
