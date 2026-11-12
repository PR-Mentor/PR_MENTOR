"""Convert static-analysis output to the shared Finding schema.

Ruff has no severity rating, so its findings use medium. Bandit HIGH, MEDIUM,
and LOW map directly; unknown values also map to medium. This is a priority
mapping only: every result remains tool_flagged until separately reproduced.
"""

from collections.abc import Iterable
from typing import Any, Literal, cast

from app.schemas.finding import Finding

Severity = Literal["info", "low", "medium", "high", "critical"]


def _severity(value: Any) -> Severity:
    normalized = value.lower() if isinstance(value, str) else ""
    if normalized in {"info", "low", "medium", "high", "critical"}:
        return cast(Severity, normalized)
    return "medium"


def _records(results: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(results, Iterable) or isinstance(results, (str, bytes, dict)):
        return ()
    return (result for result in results if isinstance(result, dict))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _line(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _path(value: Any) -> str:
    return value.replace("\\", "/") if isinstance(value, str) else ""


def normalize_ruff(results: Iterable[dict[str, Any]]) -> list[Finding]:
    return [Finding(
        file_path=_path(result.get("file_path")),
        line=_line(result.get("line")),
        rule_id=_text(result.get("rule_id")),
        severity="medium",
        message=_text(result.get("message")) or "",
        source="ruff",
        evidence=_text(result.get("evidence")),
        verification_status="tool_flagged",
    ) for result in _records(results)]


def normalize_bandit(results: Iterable[dict[str, Any]]) -> list[Finding]:
    return [Finding(
        file_path=_path(result.get("file_path")),
        line=_line(result.get("line")),
        rule_id=_text(result.get("rule_id")),
        severity=_severity(result.get("severity")),
        message=_text(result.get("message")) or "",
        source="bandit",
        evidence=_text(result.get("evidence")),
        verification_status="tool_flagged",
    ) for result in _records(results)]
