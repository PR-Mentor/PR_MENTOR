"""Deterministic pairwise analysis of generated fix proposals.

This module performs no GitHub, LLM, filesystem, or patch operations. It reports
relationships only; it never removes or combines proposals.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from app.schemas.issue import CanonicalIssue, SuggestedFix

RelationshipType = Literal[
    "duplicate", "definite_conflict", "no_detected_conflict", "incompatible_snapshot",
]

MAX_PROPOSALS_TO_ANALYZE = 25


@dataclass(frozen=True)
class _Proposal:
    canonical_issue_id: str
    fix: SuggestedFix

    @property
    def reference(self) -> tuple[str, str]:
        return self.canonical_issue_id, self.fix.fix_id


def analyze_fix_relationships(issues: list[CanonicalIssue]) -> dict[str, Any]:
    """Analyze bounded proposal pairs and mark only definite conflicts.

    Snapshot compatibility requires either the same base commit for the same
    path (with no contradictory blob hashes), or identical known file blob
    hashes. Missing hashes are acceptable only when the immutable base commit
    is the same. Incompatible same-file pairs are reported as inconclusive.
    """
    proposals = sorted(
        (
            _Proposal(issue.issue_id, fix)
            for issue in issues
            for fix in issue.suggested_fixes
        ),
        key=lambda item: (
            item.fix.file_path,
            item.fix.start_line,
            item.fix.end_line,
            item.canonical_issue_id,
            item.fix.fix_id,
        ),
    )
    total_proposals = len(proposals)
    analyzed = proposals[:MAX_PROPOSALS_TO_ANALYZE]
    skipped_count = total_proposals - len(analyzed)
    relationships: list[dict[str, Any]] = []
    conflicted_refs: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()

    for left_index, left in enumerate(analyzed):
        for right in analyzed[left_index + 1:]:
            relationship = _relationship(left, right)
            if relationship is None:
                continue
            relationships.append(relationship)
            counts[relationship["relationship_type"]] += 1
            if relationship["relationship_type"] == "definite_conflict":
                conflicted_refs.update((left.reference, right.reference))

    for proposal in analyzed:
        if proposal.reference in conflicted_refs:
            proposal.fix.generation_status = "conflicted"

    inconclusive_count = counts.get("incompatible_snapshot", 0)
    status = "partial" if skipped_count or inconclusive_count else "completed"
    if total_proposals == 0:
        status = "not_needed"
    return {
        "tool": "fix_analysis",
        "status": status,
        "executed": total_proposals > 0,
        "proposal_count": total_proposals,
        "analyzed_proposal_count": len(analyzed),
        "skipped_proposal_count": skipped_count,
        "pair_count": len(relationships),
        "relationship_counts": dict(counts),
        "relationships": relationships,
        "limitations": [
            "No detected conflict does not guarantee that disjoint proposals are independent or safe to combine.",
            "Import, symbol, function, and class dependencies are not analyzed.",
        ],
    }


def _relationship(left: _Proposal, right: _Proposal) -> dict[str, Any] | None:
    first = left.fix
    second = right.fix
    same_file = first.file_path == second.file_path

    if not same_file:
        relationship_type: RelationshipType = "no_detected_conflict"
        explanation = "The proposals target different files; this detector found no direct same-file conflict."
        method = "different_file_paths"
    elif not _snapshots_compatible(first, second):
        relationship_type = "incompatible_snapshot"
        explanation = "The proposals target the same file but their source snapshots cannot be safely compared."
        method = "base_commit_and_file_blob_snapshot_comparison"
    elif _same_range(first, second) and (
        first.expected_original_code == second.expected_original_code
        and first.proposed_code == second.proposed_code
    ):
        relationship_type = "duplicate"
        explanation = "Both proposals make the same replacement to the same source range. Both proposals are retained."
        method = "same_snapshot_exact_range_and_replacement_comparison"
    elif _ranges_overlap(first, second) and first.proposed_code != second.proposed_code:
        relationship_type = "definite_conflict"
        explanation = "The proposals overlap in the same source snapshot and prescribe different replacement code."
        method = "same_snapshot_inclusive_line_range_overlap"
    else:
        relationship_type = "no_detected_conflict"
        explanation = (
            "No direct replacement conflict was detected. Disjoint ranges are not proof of independence; "
            "overlapping ranges with identical replacement text are not inferred to be safely composable."
        )
        method = "same_snapshot_inclusive_line_range_comparison"

    refs = sorted((left.reference, right.reference))
    stable_id_material = json.dumps([refs, relationship_type], separators=(",", ":"))
    relationship_id = "fix-rel-" + hashlib.sha256(stable_id_material.encode("utf-8")).hexdigest()[:20]
    paths = sorted({first.file_path, second.file_path})
    path = first.file_path if same_file else None
    line_ranges = [
        {"canonical_issue_id": item.canonical_issue_id, "fix_id": item.fix.fix_id,
         "start_line": item.fix.start_line, "end_line": item.fix.end_line}
        for item in sorted((left, right), key=lambda proposal: proposal.reference)
    ]
    return {
        "relationship_id": relationship_id,
        "fix_ids": sorted((first.fix_id, second.fix_id)),
        "canonical_issue_ids": sorted((left.canonical_issue_id, right.canonical_issue_id)),
        "file_path": path,
        "file_paths": paths,
        "line_ranges": line_ranges,
        "relationship_type": relationship_type,
        "explanation": explanation,
        "detection_method": method,
    }


def _snapshots_compatible(first: SuggestedFix, second: SuggestedFix) -> bool:
    if first.base_commit_sha.casefold() == second.base_commit_sha.casefold():
        if first.file_blob_sha and second.file_blob_sha:
            return first.file_blob_sha.casefold() == second.file_blob_sha.casefold()
        return True
    return bool(
        first.file_blob_sha
        and second.file_blob_sha
        and first.file_blob_sha.casefold() == second.file_blob_sha.casefold()
    )


def _same_range(first: SuggestedFix, second: SuggestedFix) -> bool:
    return first.start_line == second.start_line and first.end_line == second.end_line


def _ranges_overlap(first: SuggestedFix, second: SuggestedFix) -> bool:
    return max(first.start_line, second.start_line) <= min(first.end_line, second.end_line)
