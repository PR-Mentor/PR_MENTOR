"""Common utilities for language adapters."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from app.schemas.finding import Finding, ReviewFinding
from app.schemas.issue import FindingObservation


def write_language_sources(temp_root: Path, source_files: list[tuple[str, str]]) -> list[Path]:
    """Write sanitized source files into isolated temporary directory structure."""
    targets: list[Path] = []
    for safe_path, content in source_files:
        target = temp_root / safe_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", errors="replace")
        targets.append(target)
    return targets


def build_review_finding_from_raw(
    tool_name: str,
    file_path: str,
    line: int | None,
    rule_id: str | None,
    message: str,
    severity: str,
    category: str,
    evidence: str | None = None,
    why_it_matters: str | None = None,
    suggested_fix: str | None = None,
    root: Path | None = None,
) -> ReviewFinding:
    """Construct a canonical ReviewFinding from normalized analyzer output."""
    clean_path = file_path.replace("\\", "/")
    if root is not None:
        try:
            cand = Path(clean_path)
            if not cand.is_absolute():
                cand = root / cand
            clean_path = cand.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            pass

    clean_severity = severity.lower() if severity.lower() in {
        "critical", "high", "medium", "low", "informational"
    } else "medium"

    clean_category = category.lower() if category.lower() in {
        "bug", "security", "performance", "maintainability",
        "code_quality", "testing", "documentation", "learning"
    } else "code_quality"

    evidence_items = [
        val for val in [
            f"{tool_name.title()} {rule_id}" if rule_id else None,
            evidence,
            message,
        ] if val
    ]

    return ReviewFinding(
        id=uuid4().hex,
        title=f"{tool_name.title()} {rule_id or 'finding'}",
        description=message or "Static analyzer diagnostic.",
        file_path=clean_path,
        line_start=line,
        line_end=line,
        severity=clean_severity,
        category=clean_category,
        confidence=0.75,
        source="static_analysis",
        verification_status="needs_review",
        evidence=evidence_items,
        why_it_matters=why_it_matters or "A static analyzer emitted this diagnostic. It is evidence to review, not proof of exploitability.",
        suggested_fix=suggested_fix or "Inspect the reported line and confirm the appropriate correction in context.",
        learning_explanation="Static analysis checks source patterns without executing the submitted pull request.",
        limitations=["Analyzer output can include false positives and does not establish runtime impact."],
    )


def build_observation(
    finding: ReviewFinding, tool_name: str, rule_id: str | None = None
) -> FindingObservation:
    return FindingObservation(
        original_finding=finding,
        tool_name=tool_name,
        rule_id=rule_id,
    )
