"""Central registry for multi-language analysis adapters."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from app.languages.base import LanguageAdapter


class LanguageRegistry:
    """Registry coordinating language adapters and extension routing."""

    def __init__(self) -> None:
        self._adapters_by_id: dict[str, LanguageAdapter] = {}
        self._extension_to_adapter: dict[str, LanguageAdapter] = {}

    def register(self, adapter: LanguageAdapter) -> None:
        """Register a language adapter."""
        self._adapters_by_id[adapter.language_id] = adapter
        for ext in adapter.all_extensions:
            self._extension_to_adapter[ext.lower()] = adapter

    def get_adapter(self, language_id: str) -> LanguageAdapter | None:
        return self._adapters_by_id.get(language_id.lower())

    def detect_adapter_for_path(self, filename: str) -> LanguageAdapter | None:
        norm = filename.replace("\\", "/").lower()
        suffix = PurePosixPath(norm).suffix
        return self._extension_to_adapter.get(suffix)

    def is_supported_path(self, filename: str) -> bool:
        return self.detect_adapter_for_path(filename) is not None

    def sanitize_path(self, filename: str) -> str | None:
        adapter = self.detect_adapter_for_path(filename)
        if adapter is None:
            return None
        return adapter.sanitize_path(filename)

    def partition_files_by_language(
        self, files: list[tuple[str, str]]
    ) -> dict[str, list[tuple[str, str]]]:
        """Group (safe_path, content) pairs by language_id."""
        grouped: dict[str, list[tuple[str, str]]] = {}
        for path, content in files:
            adapter = self.detect_adapter_for_path(path)
            lang = adapter.language_id if adapter else "unknown"
            grouped.setdefault(lang, []).append((path, content))
        return grouped

    def all_supported_extensions(self) -> set[str]:
        return set(self._extension_to_adapter.keys())

    def all_adapters(self) -> list[LanguageAdapter]:
        return list(self._adapters_by_id.values())

    def get_capability_matrix(self) -> dict[str, Any]:
        """Return honest capability breakdown for all registered languages."""
        return {
            adapter.language_id: {
                "display_name": adapter.display_name,
                "status": adapter.capability_status,
                "extensions": sorted(adapter.all_extensions),
                "tools": {
                    name: {
                        "status": cap.status,
                        "version": cap.version,
                        "reason": cap.reason,
                    }
                    for name, cap in adapter.detect_capabilities().items()
                },
            }
            for adapter in self._adapters_by_id.values()
        }


_GLOBAL_REGISTRY: LanguageRegistry | None = None


def get_registry() -> LanguageRegistry:
    """Get or initialize the global LanguageRegistry singleton."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        registry = LanguageRegistry()
        from app.languages.c import CAdapter
        from app.languages.cpp import CppAdapter
        from app.languages.java import JavaAdapter
        from app.languages.python import PythonAdapter

        registry.register(PythonAdapter())
        registry.register(CAdapter())
        registry.register(CppAdapter())
        registry.register(JavaAdapter())
        _GLOBAL_REGISTRY = registry
    return _GLOBAL_REGISTRY
