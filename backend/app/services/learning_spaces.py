"""Service coordinating canonical PR learning spaces, concepts, and practical coding exercises."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.database.sqlite_learning import LearningStore, get_learning_store
from app.github_service.parser import parse_pull_request_url
from app.schemas.learning_lab import (
    CodingExercise,
    ConceptStatus,
    ExerciseDraft,
    ExerciseSubmission,
    LearningConcept,
    LearningLabDashboard,
    PRLearningSpace,
)


def normalize_canonical_repo_pr(url_or_repo: str, pr_number: int | None = None) -> tuple[str, int, str]:
    """Normalize any GitHub PR URL (HTTPS, SSH, git) into (owner/repo, pr_number, canonical_url)."""
    clean = url_or_repo.strip()
    repo_match = re.search(r"github\.com[:/]([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?(?:/|$)", clean)
    if repo_match and "/pull/" not in clean and pr_number is not None:
        owner = repo_match.group(1)
        repo = repo_match.group(2)
        canonical_repo = f"{owner.lower()}/{repo.lower()}"
        canonical_url = f"https://github.com/{canonical_repo}/pull/{pr_number}"
        return canonical_repo, pr_number, canonical_url

    if clean.startswith("git@github.com:"):
        clean = "https://github.com/" + clean[len("git@github.com:"):]
        if clean.endswith(".git"):
            clean = clean[:-4]

    owner, repo, num = parse_pull_request_url(clean)
    effective_num = pr_number if pr_number is not None else num
    canonical_repo = f"{owner.lower()}/{repo.lower()}"
    canonical_url = f"https://github.com/{canonical_repo}/pull/{effective_num}"
    return canonical_repo, effective_num, canonical_url


def generate_space_id(canonical_repo: str, pr_number: int, owner_id: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", canonical_repo.lower()).strip("-")
    if owner_id:
        owner_slug = re.sub(r"[^a-z0-9]+", "-", owner_id.lower()).strip("-")
        return f"space-{slug}-{pr_number}-{owner_slug[:12]}"
    return f"space-{slug}-{pr_number}"


class LearningSpaceService:
    """Coordinates PR-derived learning spaces and coding exercises."""

    def __init__(self, store: LearningStore | None = None) -> None:
        self._store = store

    @property
    def store(self) -> LearningStore:
        if self._store is not None:
            return self._store
        return get_learning_store()

    def resolve_or_create_space(
        self, review: dict[str, Any], owner_id: str | None = None
    ) -> PRLearningSpace:
        """Resolve an existing PR learning space or create one from a review record."""
        pr_meta = review.get("pull_request", {})
        pr_url = pr_meta.get("pull_request_url") or pr_meta.get("repository_url") or ""
        num = pr_meta.get("pull_request_number") or 1
        title = pr_meta.get("title") or "Pull Request"

        canonical_repo, pr_num, canonical_url = normalize_canonical_repo_pr(pr_url, num)
        review_id = review.get("review_id", "")

        canonical_issues = review.get("canonical_issues", [])
        languages = set()
        for issue in canonical_issues:
            fp = issue.get("file_path", "").lower()
            if fp.endswith(".py"):
                languages.add("python")
            elif fp.endswith(".c") or fp.endswith(".h"):
                languages.add("c")
            elif fp.endswith(".cpp") or fp.endswith(".hpp") or fp.endswith(".cc"):
                languages.add("cpp")
            elif fp.endswith(".java"):
                languages.add("java")

        primary_lang = next(iter(languages)) if languages else "python"

        space, is_new = self.store.resolve_or_create_space_record(
            canonical_repo=canonical_repo,
            pr_number=pr_num,
            pr_title=title,
            pr_url=canonical_url,
            latest_review_id=review_id,
            primary_language=primary_lang,
            languages=sorted(languages),
            owner_id=owner_id,
        )

        self._enrich_space_from_review(space, review, owner_id)
        refreshed = self.store.get_space(space.space_id, owner_id)
        return refreshed or space

    def _enrich_space_from_review(
        self, space: PRLearningSpace, review: dict[str, Any], owner_id: str | None
    ) -> None:
        """Derive educational concepts and coding exercises from review findings."""
        canonical_issues = review.get("canonical_issues", [])
        if not canonical_issues:
            return

        concepts: list[LearningConcept] = []
        exercises: list[CodingExercise] = []

        for idx, issue in enumerate(canonical_issues[:8]):
            category = issue.get("category", "code_quality")
            desc = issue.get("normalized_description") or "Code improvement opportunity"
            file_path = issue.get("file_path", "")
            issue_id = issue.get("issue_id", "")

            lang = "python"
            fp_lower = file_path.lower()
            if fp_lower.endswith(".c") or fp_lower.endswith(".h"):
                lang = "c"
            elif fp_lower.endswith(".cpp") or fp_lower.endswith(".hpp") or fp_lower.endswith(".cc"):
                lang = "cpp"
            elif fp_lower.endswith(".java"):
                lang = "java"

            concept_id = f"concept-{space.space_id}-{idx + 1}"
            title = self._concept_title_for_issue(issue, lang)
            summary = desc
            why_it_matters = (
                issue.get("explanation", {}).get("why_it_matters")
                if isinstance(issue.get("explanation"), dict)
                else f"Addressing {category} issues ensures reliability, maintainability, and correctness in {lang.upper()} codebase."
            )

            concept = LearningConcept(
                concept_id=concept_id,
                space_id=space.space_id,
                title=title,
                summary=summary,
                why_it_matters=why_it_matters,
                category=category,
                language=lang,
                canonical_issue_ids=[issue_id] if issue_id else [],
                exercise_ids=[],
                order_index=idx + 1,
                status="not_started",
            )
            concepts.append(concept)

            exercise_id = f"ex-{concept_id}"
            exercise = self._create_exercise_for_issue(
                exercise_id, space.space_id, concept_id, issue, lang
            )
            exercises.append(exercise)

        self.store.save_concepts(concepts, owner_id)
        self.store.save_exercises(exercises)

    def _concept_title_for_issue(self, issue: dict[str, Any], lang: str) -> str:
        desc = issue.get("normalized_description", "").lower()
        cat = issue.get("category", "code_quality").lower()
        if "gets" in desc or "buffer" in desc or "strcpy" in desc:
            return "Buffer Safety & Bounds Checking"
        if "sql" in desc:
            return "SQL Injection & Query Parameterization"
        if "slicing" in desc or "exception" in desc:
            return "Safe Exception Handling & References"
        if "deserialization" in desc or "readobject" in desc:
            return "Secure Object Deserialization"
        if "shell" in desc or "subprocess" in desc or "command" in desc:
            return "Safe Subprocess & Argument Separation"
        if cat == "security":
            return f"Defensive Security Patterns ({lang.upper()})"
        return f"{cat.replace('_', ' ').title()} Principles ({lang.upper()})"

    def _create_exercise_for_issue(
        self,
        exercise_id: str,
        space_id: str,
        concept_id: str,
        issue: dict[str, Any],
        lang: str,
    ) -> CodingExercise:
        desc = issue.get("normalized_description", "")
        file_path = issue.get("file_path", "")
        cat = issue.get("category", "code_quality")

        if lang == "c":
            starter = """#include <stdio.h>
#include <string.h>

void safe_copy(const char *source, char *destination, size_t dest_size) {
    // TODO: Safely copy source into destination without exceeding dest_size.
    // Avoid using unbounded gets() or strcpy().
    
}
"""
            buggy = "strcpy(destination, source);"
            instructions = "Refactor the function to safely copy the string into destination bounded by dest_size, ensuring null-termination."
            expected = "Buffer bounds checked with strncpy or snprintf, preventing buffer overflow."
            hints = ["Use strncpy() or snprintf() with dest_size.", "Always ensure destination[dest_size - 1] = '\\0'."]
            rules = {"forbidden": ["gets(", "strcpy("], "required_any": ["strncpy", "snprintf"]}
        elif lang == "cpp":
            starter = """#include <iostream>
#include <stdexcept>
#include <string>

void process_data() {
    try {
        throw std::runtime_error("operation failed");
    }
    // TODO: Catch the exception by const reference to avoid object slicing.
    catch (/* Fill in parameter */) {
        // Handle exception safely
    }
}
"""
            buggy = "catch (std::exception e)"
            instructions = "Fix the exception catch clause to catch std::exception by const reference, preventing object slicing."
            expected = "Catch clause specifies 'const std::exception& e' or equivalent reference."
            hints = ["Catching by value slices away derived exception information.", "Use 'const std::exception&'."]
            rules = {"forbidden": ["std::exception e)", "runtime_error e)"], "required_any": ["const std::exception&", "const auto&", "const std::runtime_error&"]}
        elif lang == "java":
            starter = """package com.example.safe;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;

public class UserRepository {
    public void queryUser(Connection conn, String userId) throws SQLException {
        // TODO: Prepare and execute a parameterized query.
        // Avoid concatenating userId into the query string.
        String sql = "SELECT * FROM users WHERE id = ?";
        
    }
}
"""
            buggy = "stmt.executeQuery(\"SELECT * FROM users WHERE id = \" + userId);"
            instructions = "Use PreparedStatement with parameter binding to safely query by userId without string concatenation."
            expected = "Connection prepares statement with '?' placeholder and binds parameter using setString."
            hints = ["Use conn.prepareStatement(sql)", "stmt.setString(1, userId)"]
            rules = {"forbidden": ["createStatement()", "\" + "], "required_any": ["prepareStatement", "setString"]}
        else:
            starter = """import subprocess
from pathlib import Path

def run_tool(safe_file_path: str) -> None:
    # TODO: Safely invoke the external tool without shell=True.
    # Pass the executable and file path as an argument list.
    
    pass
"""
            buggy = "subprocess.run(f'tool {safe_file_path}', shell=True)"
            instructions = "Replace the shell-interpolated command with an argument vector list using subprocess.run(..., check=True)."
            expected = "Subprocess invoked with a list of arguments and shell=False."
            hints = ["Pass ['tool', safe_file_path]", "Do not set shell=True"]
            rules = {"forbidden": ["shell=True", "shell = True"], "required_any": ["subprocess.run([", "subprocess.call(["]}

        return CodingExercise(
            exercise_id=exercise_id,
            space_id=space_id,
            concept_id=concept_id,
            canonical_issue_id=issue.get("issue_id"),
            title=f"Practice: {issue.get('normalized_description', 'Remediate Code Issue')[:60]}",
            description=f"Based on finding in {file_path}: {desc}",
            instructions=instructions,
            language=lang,
            difficulty="intermediate" if cat == "security" else "beginner",
            category=cat if cat in ("bug", "security", "performance", "maintainability") else "code_quality",
            starter_code=starter,
            buggy_code=buggy,
            expected_behavior=expected,
            hints=hints,
            explanation=f"Applying secure coding principles in {lang.upper()} eliminates root vulnerabilities reported in the PR review.",
            completion_criteria=["Eliminate vulnerable pattern", "Implement defensive bounds/parameterization", "Ensure syntax validity"],
            validation_rules=rules,
        )

    def list_spaces(self, owner_id: str | None = None) -> list[PRLearningSpace]:
        return self.store.list_spaces(owner_id)

    def get_space(self, space_id: str, owner_id: str | None = None) -> PRLearningSpace | None:
        return self.store.get_space(space_id, owner_id)

    def list_concepts(self, space_id: str, owner_id: str | None = None) -> list[LearningConcept]:
        return self.store.list_concepts(space_id, owner_id)

    def get_concept(self, concept_id: str, owner_id: str | None = None) -> LearningConcept | None:
        return self.store.get_concept(concept_id, owner_id)

    def list_exercises(
        self, space_id: str, concept_id: str | None = None, owner_id: str | None = None
    ) -> list[CodingExercise]:
        if owner_id is not None:
            space = self.get_space(space_id, owner_id)
            if not space:
                return []
        return self.store.list_exercises(space_id, concept_id)

    def get_exercise(self, exercise_id: str) -> CodingExercise | None:
        return self.store.get_exercise(exercise_id)

    def save_draft(self, exercise_id: str, user_id: str, code: str) -> ExerciseDraft:
        return self.store.save_draft(exercise_id, user_id, code)

    def get_draft(self, exercise_id: str, user_id: str) -> ExerciseDraft | None:
        return self.store.get_draft(exercise_id, user_id)

    def submit_exercise(self, exercise_id: str, user_id: str, code: str) -> ExerciseSubmission:
        exercise = self.get_exercise(exercise_id)
        if not exercise:
            raise ValueError("Exercise not found.")

        rules = exercise.validation_rules
        forbidden = rules.get("forbidden", [])
        required = rules.get("required", [])
        required_any = rules.get("required_any", [])

        violations = [pat for pat in forbidden if pat in code]
        missing = [pat for pat in required if not any(r in code for r in (pat if isinstance(pat, list) else [pat]))]
        if required_any and not any(r in code for r in required_any):
            missing.append(f"one of {required_any}")

        passed = len(violations) == 0 and len(missing) == 0
        status = "completed" if passed else "failed"

        feedback_parts = []
        if passed:
            feedback_parts.append(
                "Excellent! Static verification confirmed the vulnerable patterns were removed and defensive patterns were implemented."
            )
            feedback_parts.append("Note: Runtime test sandbox execution is disabled in this tier for security.")
        else:
            if violations:
                feedback_parts.append(f"Vulnerable pattern still present: {', '.join(violations)}.")
            if missing:
                feedback_parts.append(f"Expected safe pattern missing: {', '.join(str(m) for m in missing)}.")
            feedback_parts.append("Review the hints and instructions to align with the expected defense.")

        feedback = " ".join(feedback_parts)
        eval_details = {
            "static_checks": "passed" if passed else "failed",
            "violations_detected": violations,
            "missing_defenses": missing,
            "runtime_execution": "disabled_by_policy",
        }

        submission = ExerciseSubmission(
            submission_id=uuid4().hex,
            exercise_id=exercise_id,
            user_id=user_id,
            code=code,
            status=status,
            passed=passed,
            feedback=feedback,
            eval_details=eval_details,
            submitted_at=datetime.now(UTC),
        )

        self.store.save_submission(submission)

        if passed:
            self.store.update_concept_status(exercise.concept_id, "mastered")
        else:
            self.store.update_concept_status(
                exercise.concept_id, "in_progress", only_if_status="not_started"
            )

        return submission

    def list_submissions(self, exercise_id: str | None, user_id: str) -> list[ExerciseSubmission]:
        return self.store.list_submissions(exercise_id, user_id)

    def get_dashboard(self, user_id: str | None = None) -> LearningLabDashboard:
        return self.store.get_dashboard(user_id)


_GLOBAL_SERVICE: LearningSpaceService | None = None


def get_learning_service() -> LearningSpaceService:
    global _GLOBAL_SERVICE
    if _GLOBAL_SERVICE is None:
        _GLOBAL_SERVICE = LearningSpaceService()
    return _GLOBAL_SERVICE
