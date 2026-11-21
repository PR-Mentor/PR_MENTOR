"""Common utilities for language adapters."""

from __future__ import annotations

import os
import re
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


def mask_c_cpp_comments(content: str) -> str:
    """Mask C/C++ comments with whitespace, preserving string and character literals.

    Preserves newlines strictly so line numbers and line counts remain identical
    to the original input. String literals, character literals, and C++ raw string
    literals are preserved intact, enabling rules that inspect literal arguments
    (such as format string checks) to operate without interference from comments.
    """
    out: list[str] = []
    i = 0
    n = len(content)
    NORMAL = 0
    LINE_COMMENT = 1
    BLOCK_COMMENT = 2
    STRING = 3
    CHAR = 4
    RAW_STRING = 5
    state = NORMAL
    raw_delim = ""

    while i < n:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < n else ""

        if state == NORMAL:
            if ch == "/" and nxt == "/":
                state = LINE_COMMENT
                out.append("  ")
                i += 2
            elif ch == "/" and nxt == "*":
                state = BLOCK_COMMENT
                out.append("  ")
                i += 2
            elif ch == "R" and nxt == '"':
                match = re.match(r'R"([^()\s\\]{0,16})\(', content[i:])
                if match:
                    raw_delim = match.group(1)
                    raw_header = match.group(0)
                    out.append(raw_header)
                    i += len(raw_header)
                    state = RAW_STRING
                else:
                    out.append(ch)
                    i += 1
            elif ch == '"':
                state = STRING
                out.append(ch)
                i += 1
            elif ch == "'":
                state = CHAR
                out.append(ch)
                i += 1
            else:
                out.append(ch)
                i += 1

        elif state == LINE_COMMENT:
            if ch in ("\n", "\r"):
                state = NORMAL
                out.append(ch)
            else:
                out.append(" ")
            i += 1

        elif state == BLOCK_COMMENT:
            if ch == "*" and nxt == "/":
                state = NORMAL
                out.append("  ")
                i += 2
            elif ch in ("\n", "\r"):
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1

        elif state == STRING:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
            elif ch == '"':
                state = NORMAL
                i += 1
            else:
                i += 1

        elif state == CHAR:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
            elif ch == "'":
                state = NORMAL
                i += 1
            else:
                i += 1

        elif state == RAW_STRING:
            closing = f'){raw_delim}"'
            if content[i:].startswith(closing):
                out.append(closing)
                i += len(closing)
                state = NORMAL
            else:
                out.append(ch)
                i += 1

    return "".join(out)


def mask_c_cpp_comments_and_strings(content: str) -> str:
    """Mask C/C++ comments and string/character literals with whitespace.

    Preserves newlines strictly so line numbers and line counts remain identical
    to the original input. Non-comment and non-literal code remains in its exact
    column position, preventing false positives from keywords/patterns occurring
    inside comments or string literals.
    """
    out: list[str] = []
    i = 0
    n = len(content)
    NORMAL = 0
    LINE_COMMENT = 1
    BLOCK_COMMENT = 2
    STRING = 3
    CHAR = 4
    RAW_STRING = 5
    state = NORMAL
    raw_delim = ""

    while i < n:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < n else ""

        if state == NORMAL:
            if ch == "/" and nxt == "/":
                state = LINE_COMMENT
                out.append("  ")
                i += 2
            elif ch == "/" and nxt == "*":
                state = BLOCK_COMMENT
                out.append("  ")
                i += 2
            elif ch == "R" and nxt == '"':
                match = re.match(r'R"([^()\s\\]{0,16})\(', content[i:])
                if match:
                    raw_delim = match.group(1)
                    raw_header = match.group(0)
                    out.append(" " * len(raw_header))
                    i += len(raw_header)
                    state = RAW_STRING
                else:
                    out.append(ch)
                    i += 1
            elif ch == '"':
                state = STRING
                out.append(" ")
                i += 1
            elif ch == "'":
                state = CHAR
                out.append(" ")
                i += 1
            else:
                out.append(ch)
                i += 1

        elif state == LINE_COMMENT:
            if ch in ("\n", "\r"):
                state = NORMAL
                out.append(ch)
            else:
                out.append(" ")
            i += 1

        elif state == BLOCK_COMMENT:
            if ch == "*" and nxt == "/":
                state = NORMAL
                out.append("  ")
                i += 2
            elif ch in ("\n", "\r"):
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1

        elif state == STRING:
            if ch == "\\" and i + 1 < n:
                if nxt in ("\n", "\r"):
                    out.append(" ")
                    out.append(nxt)
                else:
                    out.append("  ")
                i += 2
            elif ch == '"':
                state = NORMAL
                out.append(" ")
                i += 1
            elif ch in ("\n", "\r"):
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1

        elif state == CHAR:
            if ch == "\\" and i + 1 < n:
                if nxt in ("\n", "\r"):
                    out.append(" ")
                    out.append(nxt)
                else:
                    out.append("  ")
                i += 2
            elif ch == "'":
                state = NORMAL
                out.append(" ")
                i += 1
            elif ch in ("\n", "\r"):
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1

        elif state == RAW_STRING:
            closing = f'){raw_delim}"'
            if content[i:].startswith(closing):
                out.append(" " * len(closing))
                i += len(closing)
                state = NORMAL
            elif ch in ("\n", "\r"):
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1

    return "".join(out)
