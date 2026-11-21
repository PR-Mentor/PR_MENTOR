"""Centralized structured output validation and evidence-grounding verification."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


def extract_and_parse_json(raw_text: str) -> dict[str, Any]:
    """Parse JSON safely from raw LLM output, stripping markdown code fences if present."""
    text = raw_text.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fences
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        # Bounded repair: find outermost { ... } if preamble exists
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                raise ValueError(f"AI response contains malformed JSON: {exc}") from exc
        else:
            raise ValueError(f"AI response is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a single JSON object.")
    return parsed


def validate_pydantic_model(data: dict[str, Any] | str, model_cls: type[T]) -> T:
    """Validate JSON data against a Pydantic model with descriptive error on failure."""
    if isinstance(data, str):
        data = extract_and_parse_json(data)
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        missing_fields = [e["loc"][0] for e in exc.errors() if e["type"] == "missing"]
        if missing_fields:
            raise ValueError(f"AI response missing required schema fields: {missing_fields}") from exc
        raise ValueError(f"AI response schema validation failed: {exc}") from exc


def verify_evidence_grounding(
    file_path: str,
    line_start: int | None,
    line_end: int | None,
    diff_files: list[dict[str, Any]],
    evidence: list[str],
) -> bool:
    """Ensure candidate or explanation references actual files and modified lines in the PR diff."""
    raw_path = file_path.replace("\\", "/")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in path.parts[0]:
        return False
    normalized_path = path.as_posix()

    added_lines: dict[int, str] = {}
    for changed_file in diff_files:
        candidate_path = changed_file.get("path")
        if not isinstance(candidate_path, str):
            continue
        normalized_changed_path = PurePosixPath(candidate_path.replace("\\", "/")).as_posix()
        if normalized_changed_path != normalized_path:
            continue
        for hunk in changed_file.get("hunks", []):
            if not isinstance(hunk, dict):
                continue
            for line in hunk.get("lines", []):
                if (
                    isinstance(line, dict)
                    and line.get("kind") == "added"
                    and isinstance(line.get("line"), int)
                    and isinstance(line.get("text"), str)
                ):
                    added_lines[line["line"]] = line["text"]
        break

    if not added_lines or not evidence:
        return False

    start = line_start if line_start is not None else line_end
    end = line_end if line_end is not None else line_start
    if start is not None and end is not None:
        if start < 1 or end < start:
            return False
        relevant_lines = [text for number, text in added_lines.items() if start <= number <= end]
        if not relevant_lines:
            return False
    else:
        relevant_lines = list(added_lines.values())

    source_text = "\n".join(" ".join(line.split()).casefold() for line in relevant_lines)
    return any(
        len(normalized := " ".join(item.split()).casefold()) >= 8
        and normalized in source_text
        for item in evidence
    )
