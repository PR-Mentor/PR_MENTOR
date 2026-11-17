"""Base contracts for multi-language analysis adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from app.schemas.finding import ReviewFinding
from app.schemas.issue import FindingObservation

LanguageCapabilityStatus = Literal["planned", "experimental", "partial", "production"]
ToolAvailabilityStatus = Literal["available", "unavailable", "failed", "completed", "skipped"]


@dataclass(frozen=True)
class ToolCapability:
    """Explicit capability and availability status for an analysis tool."""

    tool_name: str
    status: ToolAvailabilityStatus
    version: str | None = None
    reason: str | None = None


class LanguageAdapter(ABC):
    """Abstract interface implemented by all language-specific review adapters."""

    @property
    @abstractmethod
    def language_id(self) -> str:
        """Normalized unique identifier (e.g. 'python', 'c', 'cpp', 'java')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g. 'Python', 'C', 'C++', 'Java')."""

    @property
    @abstractmethod
    def capability_status(self) -> LanguageCapabilityStatus:
        """Honest capability level: planned, experimental, partial, or production."""

    @property
    @abstractmethod
    def source_extensions(self) -> set[str]:
        """Set of lowercase file extensions for source files (e.g. {'.py'})."""

    @property
    def header_extensions(self) -> set[str]:
        """Set of lowercase file extensions for header files (e.g. {'.h'})."""
        return set()

    @property
    def all_extensions(self) -> set[str]:
        return self.source_extensions | self.header_extensions

    def is_source_path(self, filename: str) -> bool:
        norm = filename.replace("\\", "/").lower()
        suffix = PurePosixPath(norm).suffix
        return suffix in self.source_extensions

    def is_header_path(self, filename: str) -> bool:
        norm = filename.replace("\\", "/").lower()
        suffix = PurePosixPath(norm).suffix
        return suffix in self.header_extensions

    def is_supported_path(self, filename: str) -> bool:
        norm = filename.replace("\\", "/").lower()
        suffix = PurePosixPath(norm).suffix
        return suffix in self.all_extensions

    def sanitize_path(self, filename: str) -> str | None:
        """Validate and return safe posix path if supported by this language."""
        normalized = filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or "\x00" in normalized
            or ":" in path.parts[0]
            or path.suffix.lower() not in self.all_extensions
        ):
            return None
        return path.as_posix()

    @abstractmethod
    def detect_capabilities(self) -> dict[str, ToolCapability]:
        """Probe environment for installed tools and return capability mapping."""

    @abstractmethod
    async def run_static_analysis(
        self,
        source_files: list[tuple[str, str]],
        workspace_dir: Path,
    ) -> tuple[list[ReviewFinding], list[FindingObservation], list[dict[str, Any]]]:
        """Run configured static and security analyzers on staged source files.

        Returns:
            - normalized_findings: list of ReviewFinding
            - observations: list of FindingObservation
            - tool_results: list of tool execution status dicts
        """

    @abstractmethod
    def summarize_sources(self, source_files: list[tuple[str, str]]) -> dict[str, Any]:
        """Extract high-level structural intelligence (modules, functions, classes)."""
