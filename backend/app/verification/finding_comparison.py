"""Conservative deterministic comparison of analyzer finding multisets."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Literal

DeltaCategory = Literal["introduced", "resolved", "unchanged", "modified", "uncertain"]


@dataclass(frozen=True)
class FindingDelta:
    delta_id: str
    category: DeltaCategory
    baseline: dict[str, Any] | None
    proposed: dict[str, Any] | None
    confidence: Literal["high", "low"]
    relocated: bool = False
    candidate_count: int = 0
    attribution: str = "unknown"

    def model_data(self) -> dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "category": self.category,
            "baseline": self.baseline,
            "proposed": self.proposed,
            "confidence": self.confidence,
            "relocated": self.relocated,
            "candidate_count": self.candidate_count,
            "attribution": self.attribution,
        }


def compare_findings(
    baseline: list[dict[str, Any]], proposed: list[dict[str, Any]]
) -> list[FindingDelta]:
    """Match exact identities first; emit uncertainty instead of weak guesses.

    Locations are excluded from the exact identity, so line shifts do not turn
    an otherwise identical diagnostic into an introduced finding. Modified
    matches require a unique remaining same-tool/path/rule/message candidate.
    """
    old_items = _ordered(baseline)
    new_items = _ordered(proposed)
    old_used: set[int] = set()
    new_used: set[int] = set()
    deltas: list[FindingDelta] = []

    old_by_signature: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    new_by_signature: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, item in enumerate(old_items):
        old_by_signature[_identity(item)].append(index)
    for index, item in enumerate(new_items):
        new_by_signature[_identity(item)].append(index)

    for signature in sorted(set(old_by_signature) & set(new_by_signature), key=repr):
        old_group = old_by_signature[signature]
        new_group = new_by_signature[signature]
        for ordinal, (old_index, new_index) in enumerate(
            zip(old_group, new_group), start=1
        ):
            old_used.add(old_index)
            new_used.add(new_index)
            deltas.append(
                _delta(
                    "unchanged",
                    old_items[old_index],
                    new_items[new_index],
                    "high",
                    relocated=_location(old_items[old_index])
                    != _location(new_items[new_index]),
                    ordinal=ordinal,
                )
            )

    old_remaining = [i for i in range(len(old_items)) if i not in old_used]
    new_remaining = [i for i in range(len(new_items)) if i not in new_used]
    old_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    new_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index in old_remaining:
        if old_items[index].get("rule_id") is not None:
            old_groups[_rule_identity(old_items[index])].append(index)
    for index in new_remaining:
        if new_items[index].get("rule_id") is not None:
            new_groups[_rule_identity(new_items[index])].append(index)

    for key in sorted(set(old_groups) & set(new_groups), key=repr):
        old_group, new_group = old_groups[key], new_groups[key]
        if len(old_group) == len(new_group) == 1:
            old_index, new_index = old_group[0], new_group[0]
            old_used.add(old_index)
            new_used.add(new_index)
            deltas.append(
                _delta(
                    "modified",
                    old_items[old_index],
                    new_items[new_index],
                    "high",
                    relocated=_location(old_items[old_index])
                    != _location(new_items[new_index]),
                )
            )
        else:
            # Repeated diagnostics sharing only tool/path/rule do not provide
            # enough information for deterministic one-to-one pairing.
            for ordinal, old_index in enumerate(old_group, start=1):
                old_used.add(old_index)
                deltas.append(
                    _delta(
                        "uncertain",
                        old_items[old_index],
                        None,
                        "low",
                        candidate_count=len(new_group),
                        ordinal=ordinal,
                    )
                )
            for ordinal, new_index in enumerate(new_group, start=1):
                new_used.add(new_index)
                deltas.append(
                    _delta(
                        "uncertain",
                        None,
                        new_items[new_index],
                        "low",
                        candidate_count=len(old_group),
                        ordinal=ordinal,
                    )
                )

    old_remaining = [i for i in range(len(old_items)) if i not in old_used]
    new_remaining = [i for i in range(len(new_items)) if i not in new_used]
    uncertain_old_ordinals: Counter[tuple[Any, ...]] = Counter()
    for old_index in old_remaining:
        old_item = old_items[old_index]
        candidates = [
            index
            for index in new_remaining
            if _plausible_candidate(old_item, new_items[index])
        ]
        if candidates:
            old_used.add(old_index)
            ordinal_key = _identity(old_item)
            uncertain_old_ordinals[ordinal_key] += 1
            deltas.append(
                _delta(
                    "uncertain",
                    old_item,
                    None,
                    "low",
                    candidate_count=len(candidates),
                    ordinal=uncertain_old_ordinals[ordinal_key],
                )
            )
    uncertain_new_ordinals: Counter[tuple[Any, ...]] = Counter()
    for new_index in new_remaining:
        if new_index in new_used:
            continue
        new_item = new_items[new_index]
        candidates = [
            index
            for index in old_remaining
            if index in old_used and _plausible_candidate(old_items[index], new_item)
        ]
        # Existing uncertain baseline records carry candidate counts. A
        # conservative reverse lookup prevents claiming introduction.
        if not candidates:
            candidates = [
                index
                for index in old_remaining
                if _plausible_candidate(old_items[index], new_item)
            ]
        if candidates:
            new_used.add(new_index)
            ordinal_key = _identity(new_item)
            uncertain_new_ordinals[ordinal_key] += 1
            deltas.append(
                _delta(
                    "uncertain",
                    None,
                    new_item,
                    "low",
                    candidate_count=len(candidates),
                    ordinal=uncertain_new_ordinals[ordinal_key],
                )
            )

    resolved_ordinals: Counter[tuple[Any, ...]] = Counter()
    for old_index, item in enumerate(old_items):
        if old_index not in old_used:
            key = _identity(item)
            resolved_ordinals[key] += 1
            deltas.append(
                _delta("resolved", item, None, "high", ordinal=resolved_ordinals[key])
            )
    introduced_ordinals: Counter[tuple[Any, ...]] = Counter()
    for new_index, item in enumerate(new_items):
        if new_index not in new_used:
            key = _identity(item)
            introduced_ordinals[key] += 1
            deltas.append(
                _delta(
                    "introduced", None, item, "high", ordinal=introduced_ordinals[key]
                )
            )

    return sorted(
        deltas,
        key=lambda item: (
            item.category,
            _location(item.baseline or item.proposed or {}),
            item.delta_id,
        ),
    )


def _ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            _location(item),
            str(item.get("rule_id") or ""),
            _normalized(item.get("message")),
            _normalized(item.get("context")),
            str(item.get("severity") or ""),
            str(item.get("confidence") or ""),
        ),
    )


def _identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("tool"),
        item.get("file_path"),
        item.get("rule_id"),
        _normalized(item.get("message")),
        _normalized_context(item.get("context")),
        _normalized(item.get("severity")),
        _normalized(item.get("confidence")),
    )


def _rule_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("tool"),
        item.get("file_path"),
        item.get("rule_id"),
    )


def _plausible_candidate(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if old.get("tool") != new.get("tool") or old.get("file_path") != new.get(
        "file_path"
    ):
        return False
    old_rule, new_rule = old.get("rule_id"), new.get("rule_id")
    if old_rule is not None and new_rule is not None:
        return old_rule == new_rule
    return old_rule is None or new_rule is None


def _normalized(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()).casefold()


def _normalized_context(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lines = []
    for line in value.splitlines():
        line = re.sub(r"^\s*\d+\s*(?:>>|>)?\s?", "", line)
        lines.append(" ".join(line.split()))
    return "\n".join(lines).casefold()


def _location(item: dict[str, Any]) -> tuple[int, int]:
    line = item.get("line")
    column = item.get("column")
    return (
        line if isinstance(line, int) and not isinstance(line, bool) else 0,
        column if isinstance(column, int) and not isinstance(column, bool) else 0,
    )


def _delta(
    category: DeltaCategory,
    baseline: dict[str, Any] | None,
    proposed: dict[str, Any] | None,
    confidence: Literal["high", "low"],
    *,
    relocated: bool = False,
    candidate_count: int = 0,
    ordinal: int = 0,
) -> FindingDelta:
    material = {
        "category": category,
        "baseline": _identity(baseline) if baseline else None,
        "proposed": _identity(proposed) if proposed else None,
        "baseline_line": _location(baseline) if baseline else None,
        "proposed_line": _location(proposed) if proposed else None,
        "ordinal": ordinal,
        "candidate_count": candidate_count,
    }
    digest = hashlib.sha256(
        json.dumps(
            material, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()[:24]
    return FindingDelta(
        f"delta-{digest}",
        category,
        baseline,
        proposed,
        confidence,
        relocated=relocated,
        candidate_count=candidate_count,
    )
