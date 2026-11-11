"""Conservative, deterministic grouping of finding observations into issues."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.schemas.finding import ReviewFinding
from app.schemas.issue import CanonicalIssue, FindingObservation

_TOKEN = re.compile(r"[^\w]+", re.UNICODE)
_SEVERITY_RANK = {"informational": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_SIMILARITY_THRESHOLD = 0.82
_AI_SIMILARITY_THRESHOLD = 0.90


@dataclass(frozen=True)
class ReconciliationResult:
    """Canonical issues plus raw and unique counts for a review."""

    issues: list[CanonicalIssue]
    unique_issue_count: int
    observation_count: int


def reconcile_observations(observations: Iterable[FindingObservation]) -> ReconciliationResult:
    """Group only observations with strong, deterministic evidence in common.

    Paths and categories must match. Known line ranges must overlap. Conflicting
    non-empty rule IDs never match. AI observations without a rule ID require
    shared evidence as well as matching line information and issue meaning.
    Missing locations are only reconciled when the rule, normalized message,
    and evidence all match, or when the exact same source finding was repeated.
    """

    ordered = sorted(observations, key=_observation_sort_key)
    groups: list[list[FindingObservation]] = []
    for observation in ordered:
        matches = [group for group in groups if all(_same_issue(observation, member) for member in group)]
        # If the observation ambiguously matches more than one group, leave it
        # separate instead of choosing a potentially incorrect canonical issue.
        if len(matches) == 1:
            matches[0].append(observation)
        else:
            groups.append([observation])

    issues = [_build_issue(group) for group in groups]
    issues.sort(key=lambda issue: issue.issue_id)
    return ReconciliationResult(
        issues=issues,
        unique_issue_count=len(issues),
        observation_count=sum(len(issue.observations) for issue in issues),
    )


def _same_issue(left: FindingObservation, right: FindingObservation) -> bool:
    left_finding = left.original_finding
    right_finding = right.original_finding
    if _normalize_path(left_finding.file_path) != _normalize_path(right_finding.file_path):
        return False
    if left_finding.category != right_finding.category:
        return False

    left_rule = _normalize_rule(left.rule_id)
    right_rule = _normalize_rule(right.rule_id)
    if left_rule and right_rule and left_rule != right_rule:
        return False

    left_text = _normalize_text(left_finding.description)
    right_text = _normalize_text(right_finding.description)
    same_text = bool(left_text) and left_text == right_text
    same_location = _locations_overlap(left_finding, right_finding)
    same_evidence = _evidence_overlaps(left_finding, right_finding)
    has_ai_without_rule = (
        (left_finding.source == "ai_candidate" and not left_rule)
        or (right_finding.source == "ai_candidate" and not right_rule)
    )

    if same_location is None:
        # Without line data, identical text alone can describe repeated generic
        # issues. Require a shared rule, exact message, and shared evidence, or
        # a repeated copy of the same original diagnostic from the same source.
        same_original = (
            left_finding.id == right_finding.id
            and left_finding.source == right_finding.source
            and same_text
        )
        return same_original or (bool(left_rule and left_rule == right_rule) and same_text and same_evidence)
    if not same_location:
        return False

    similarity = _text_similarity(left_text, right_text)
    if has_ai_without_rule:
        # AI has no stable rule identifier to corroborate the match. Require
        # location, shared evidence, and either exact wording or very high
        # lexical similarity.
        return same_evidence and (same_text or similarity >= _AI_SIMILARITY_THRESHOLD)

    if left_rule and right_rule:
        return same_text or similarity >= _SIMILARITY_THRESHOLD

    # When at least one side lacks a rule, require shared evidence and a strong
    # description match; proximity by itself is never enough.
    return same_evidence and (same_text or similarity >= _SIMILARITY_THRESHOLD)


def _locations_overlap(left: ReviewFinding, right: ReviewFinding) -> bool | None:
    left_range = _line_range(left)
    right_range = _line_range(right)
    if left_range is None or right_range is None:
        return None
    return max(left_range[0], right_range[0]) <= min(left_range[1], right_range[1])


def _line_range(finding: ReviewFinding) -> tuple[int, int] | None:
    if finding.line_start is None and finding.line_end is None:
        return None
    start = finding.line_start if finding.line_start is not None else finding.line_end
    end = finding.line_end if finding.line_end is not None else finding.line_start
    if start is None or end is None:
        return None
    return min(start, end), max(start, end)


def _evidence_overlaps(left: ReviewFinding, right: ReviewFinding) -> bool:
    left_evidence = {_normalize_text(item) for item in left.evidence if _normalize_text(item)}
    right_evidence = {_normalize_text(item) for item in right.evidence if _normalize_text(item)}
    return bool(left_evidence & right_evidence)


def _text_similarity(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _normalize_text(value: str) -> str:
    return " ".join(_TOKEN.sub(" ", value.casefold()).split())


def _normalize_display_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_path(value: str) -> str:
    # PurePosixPath normalizes slash direction, repeated separators and '.'.
    # Keep case and '..' segments unchanged to avoid merging distinct or unsafe
    # paths through case folding or traversal resolution.
    normalized = value.replace("\\", "/")
    return PurePosixPath(normalized).as_posix() if normalized else ""


def _normalize_rule(value: str | None) -> str:
    return value.strip().casefold() if value and value.strip() else ""


def _observation_sort_key(observation: FindingObservation) -> tuple[str, ...]:
    finding = observation.original_finding
    start, end = _line_range(finding) or (0, 0)
    return (
        _normalize_path(finding.file_path),
        finding.category,
        f"{start:010d}",
        f"{end:010d}",
        _normalize_rule(observation.rule_id),
        _normalize_text(finding.description),
        finding.source,
        finding.id,
    )


def _build_issue(group: list[FindingObservation]) -> CanonicalIssue:
    # A rule-bearing, located observation is the most stable identity anchor;
    # otherwise use a located message, then the original finding ID.
    anchor = min(group, key=_identity_anchor_sort_key)
    anchor_finding = anchor.original_finding
    paths = {_normalize_path(item.original_finding.file_path) for item in group}
    path = next(iter(paths))
    ranges = [_line_range(item.original_finding) for item in group]
    known_ranges = [value for value in ranges if value is not None]
    line_start = min((value[0] for value in known_ranges), default=None)
    line_end = max((value[1] for value in known_ranges), default=None)
    severity = max(
        (item.original_finding.severity for item in group),
        key=lambda value: _SEVERITY_RANK[value],
    )
    normalized_description = _normalize_display_text(anchor_finding.description)
    issue_id = _stable_issue_id(anchor, path, normalized_description)
    return CanonicalIssue(
        issue_id=issue_id,
        file_path=path,
        line_start=line_start,
        line_end=line_end,
        category=anchor_finding.category,
        severity=severity,
        normalized_description=normalized_description,
        observations=group,
    )


def _identity_anchor_sort_key(observation: FindingObservation) -> tuple[int, tuple[str, ...]]:
    finding = observation.original_finding
    located = finding.line_start is not None or finding.line_end is not None
    has_rule = bool(_normalize_rule(observation.rule_id))
    rank = 0 if has_rule and located else 1 if located else 2 if has_rule else 3
    return rank, _observation_sort_key(observation)


def _stable_issue_id(anchor: FindingObservation, path: str, description: str) -> str:
    finding = anchor.original_finding
    line_range = _line_range(finding)
    rule_id = _normalize_rule(anchor.rule_id)
    identity = {
        "path": path,
        "category": finding.category,
        "rule_id": rule_id,
        "line": line_range,
        "description": _normalize_text(description),
    }
    if line_range is None and not rule_id:
        # With neither a rule nor location, the original source ID is the only
        # reliable discriminator between otherwise indistinguishable issues.
        identity["finding_id"] = finding.id
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "issue-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
