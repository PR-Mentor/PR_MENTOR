"""PR-suite status is reported by GitHub Actions, never executed by the API."""

from typing import Any


def run_tests(*, target: str | None = None, command: str | None = None) -> dict[str, Any]:
    """Return a neutral placeholder while the review is persisted and Actions is queried."""
    del target, command  # Inputs are intentionally not executed or interpreted.
    return {
        "status": "pending",
        "executed": False,
        "message": "PR test results are retrieved from GitHub Actions for this pull request.",
        "findings": [],
    }
