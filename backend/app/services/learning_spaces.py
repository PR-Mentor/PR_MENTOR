"""Service coordinating canonical PR learning spaces, concepts, and practical coding exercises."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.database.connection import connection
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


@contextmanager
def _get_db():
    with connection() as db:
        yield db


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


def generate_space_id(canonical_repo: str, pr_number: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", canonical_repo.lower()).strip("-")
    return f"space-{slug}-{pr_number}"


class LearningSpaceService:
    """Coordinates PR-derived learning spaces and coding exercises."""

    def resolve_or_create_space(
        self, review: dict[str, Any], owner_id: str | None = None
    ) -> PRLearningSpace:
        """Resolve an existing PR learning space or create one from a review record."""
        pr_meta = review.get("pull_request", {})
        pr_url = pr_meta.get("pull_request_url") or pr_meta.get("repository_url") or ""
        num = pr_meta.get("pull_request_number") or 1
        title = pr_meta.get("title") or "Pull Request"

        canonical_repo, pr_num, canonical_url = normalize_canonical_repo_pr(pr_url, num)
        space_id = generate_space_id(canonical_repo, pr_num)
        review_id = review.get("review_id", "")

        now_str = datetime.now(UTC).isoformat()
        with _get_db() as db:
            row = db.execute(
                "SELECT * FROM learning_spaces WHERE space_id = ? AND (? IS NULL OR owner_id = ?)",
                (space_id, owner_id, owner_id),
            ).fetchone()

            if row:
                review_count = row["review_count"] + 1
                db.execute(
                    """
                    UPDATE learning_spaces
                    SET latest_review_id = ?, review_count = ?, updated_at = ?
                    WHERE space_id = ?
                    """,
                    (review_id, review_count, now_str, space_id),
                )
            else:
                # Detect primary language from canonical issues or default to python
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

                db.execute(
                    """
                    INSERT OR REPLACE INTO learning_spaces
                    (space_id, canonical_repo, pr_number, pr_title, pr_url, latest_review_id, review_count,
                     primary_language, languages_json, created_at, updated_at, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        space_id,
                        canonical_repo,
                        pr_num,
                        title,
                        canonical_url,
                        review_id,
                        primary_lang,
                        json.dumps(sorted(languages)),
                        now_str,
                        now_str,
                        owner_id,
                    ),
                )

        space = self.get_space(space_id, owner_id)
        if space:
            self._enrich_space_from_review(space, review, owner_id)
            return space
        if space:
            self._enrich_space_from_review(space, review, owner_id)
            return space

        return PRLearningSpace(
            space_id=space_id,
            canonical_repo=canonical_repo,
            pr_number=pr_num,
            pr_title=title,
            pr_url=canonical_url,
            latest_review_id=review_id,
            primary_language=primary_lang,
            languages=sorted(languages),
        )

    def _enrich_space_from_review(
        self, space: PRLearningSpace, review: dict[str, Any], owner_id: str | None
    ) -> None:
        """Derive educational concepts and coding exercises from review findings."""
        canonical_issues = review.get("canonical_issues", [])
        if not canonical_issues:
            return

        with _get_db() as db:
            for idx, issue in enumerate(canonical_issues[:8]):
                category = issue.get("category", "code_quality")
                desc = issue.get("normalized_description") or "Code improvement opportunity"
                file_path = issue.get("file_path", "")
                issue_id = issue.get("issue_id", "")
                obs = issue.get("observations", [])
                evidence = [
                    e for o in obs for e in o.get("original_finding", {}).get("evidence", [])
                ][:3]

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

                db.execute(
                    """
                    INSERT OR IGNORE INTO learning_concepts
                    (concept_id, space_id, title, summary, why_it_matters, category, language,
                     canonical_issue_ids_json, exercise_ids_json, order_index, status, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_started', ?)
                    """,
                    (
                        concept_id,
                        space.space_id,
                        title,
                        summary,
                        why_it_matters,
                        category,
                        lang,
                        json.dumps([issue_id]),
                        json.dumps([]),
                        idx + 1,
                        owner_id,
                    ),
                )

                exercise_id = f"ex-{concept_id}"
                exercise = self._create_exercise_for_issue(
                    exercise_id, space.space_id, concept_id, issue, lang
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO learning_exercises
                    (exercise_id, space_id, concept_id, canonical_issue_id, title, description,
                     instructions, language, difficulty, category, starter_code, buggy_code,
                     expected_behavior, hints_json, explanation, completion_criteria_json,
                     validation_rules_json, points)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exercise.exercise_id,
                        exercise.space_id,
                        exercise.concept_id,
                        exercise.canonical_issue_id,
                        exercise.title,
                        exercise.description,
                        exercise.instructions,
                        exercise.language,
                        exercise.difficulty,
                        exercise.category,
                        exercise.starter_code,
                        exercise.buggy_code,
                        exercise.expected_behavior,
                        json.dumps(exercise.hints),
                        exercise.explanation,
                        json.dumps(exercise.completion_criteria),
                        json.dumps(exercise.validation_rules),
                        exercise.points,
                    ),
                )

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
        with _get_db() as db:
            rows = db.execute(
                "SELECT * FROM learning_spaces WHERE (? IS NULL OR owner_id = ?) ORDER BY updated_at DESC",
                (owner_id, owner_id),
            ).fetchall()
        result = []
        for r in rows:
            langs = []
            try:
                langs = json.loads(r["languages_json"] or "[]")
            except Exception:
                pass
            result.append(
                PRLearningSpace(
                    space_id=r["space_id"],
                    canonical_repo=r["canonical_repo"],
                    pr_number=r["pr_number"],
                    pr_title=r["pr_title"] or "Pull Request",
                    pr_url=r["pr_url"],
                    latest_review_id=r["latest_review_id"] or "",
                    review_count=r["review_count"],
                    primary_language=r["primary_language"] or "python",
                    languages=langs,
                    created_at=datetime.fromisoformat(r["created_at"]),
                    updated_at=datetime.fromisoformat(r["updated_at"]),
                )
            )
        return result

    def get_space(self, space_id: str, owner_id: str | None = None) -> PRLearningSpace | None:
        with _get_db() as db:
            row = db.execute(
                "SELECT * FROM learning_spaces WHERE space_id = ? AND (? IS NULL OR owner_id = ?)",
                (space_id, owner_id, owner_id),
            ).fetchone()
        if not row:
            return None
        langs = []
        try:
            langs = json.loads(row["languages_json"] or "[]")
        except Exception:
            pass
        return PRLearningSpace(
            space_id=row["space_id"],
            canonical_repo=row["canonical_repo"],
            pr_number=row["pr_number"],
            pr_title=row["pr_title"] or "Pull Request",
            pr_url=row["pr_url"],
            latest_review_id=row["latest_review_id"] or "",
            review_count=row["review_count"],
            primary_language=row["primary_language"] or "python",
            languages=langs,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_concepts(self, space_id: str, owner_id: str | None = None) -> list[LearningConcept]:
        with _get_db() as db:
            rows = db.execute(
                "SELECT * FROM learning_concepts WHERE space_id = ? AND (? IS NULL OR owner_id = ?) ORDER BY order_index ASC",
                (space_id, owner_id, owner_id),
            ).fetchall()
        result = []
        for r in rows:
            issues = []
            exercises = []
            try:
                issues = json.loads(r["canonical_issue_ids_json"] or "[]")
                exercises = json.loads(r["exercise_ids_json"] or "[]")
            except Exception:
                pass
            result.append(
                LearningConcept(
                    concept_id=r["concept_id"],
                    space_id=r["space_id"],
                    title=r["title"],
                    summary=r["summary"] or "",
                    why_it_matters=r["why_it_matters"] or "",
                    category=r["category"] or "code_quality",
                    language=r["language"] or "python",
                    canonical_issue_ids=issues,
                    exercise_ids=exercises,
                    order_index=r["order_index"],
                    status=r["status"] or "not_started",
                )
            )
        return result

    def get_concept(self, concept_id: str, owner_id: str | None = None) -> LearningConcept | None:
        with _get_db() as db:
            row = db.execute(
                "SELECT * FROM learning_concepts WHERE concept_id = ? AND (? IS NULL OR owner_id = ?)",
                (concept_id, owner_id, owner_id),
            ).fetchone()
        if not row:
            return None
        issues = []
        exercises = []
        try:
            issues = json.loads(row["canonical_issue_ids_json"] or "[]")
            exercises = json.loads(row["exercise_ids_json"] or "[]")
        except Exception:
            pass
        return LearningConcept(
            concept_id=row["concept_id"],
            space_id=row["space_id"],
            title=row["title"],
            summary=row["summary"] or "",
            why_it_matters=row["why_it_matters"] or "",
            category=row["category"] or "code_quality",
            language=row["language"] or "python",
            canonical_issue_ids=issues,
            exercise_ids=exercises,
            order_index=row["order_index"],
            status=row["status"] or "not_started",
        )

    def list_exercises(self, space_id: str, concept_id: str | None = None) -> list[CodingExercise]:
        with _get_db() as db:
            if concept_id:
                rows = db.execute(
                    "SELECT * FROM learning_exercises WHERE space_id = ? AND concept_id = ?",
                    (space_id, concept_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM learning_exercises WHERE space_id = ?",
                    (space_id,),
                ).fetchall()
        result = []
        for r in rows:
            hints = []
            criteria = []
            rules = {}
            try:
                hints = json.loads(r["hints_json"] or "[]")
                criteria = json.loads(r["completion_criteria_json"] or "[]")
                rules = json.loads(r["validation_rules_json"] or "{}")
            except Exception:
                pass
            result.append(
                CodingExercise(
                    exercise_id=r["exercise_id"],
                    space_id=r["space_id"],
                    concept_id=r["concept_id"],
                    canonical_issue_id=r["canonical_issue_id"],
                    title=r["title"],
                    description=r["description"] or "",
                    instructions=r["instructions"] or "",
                    language=r["language"],
                    difficulty=r["difficulty"] or "beginner",
                    category=r["category"] or "code_quality",
                    starter_code=r["starter_code"] or "",
                    buggy_code=r["buggy_code"],
                    expected_behavior=r["expected_behavior"] or "",
                    hints=hints,
                    explanation=r["explanation"] or "",
                    completion_criteria=criteria,
                    validation_rules=rules,
                    points=r["points"] or 10,
                )
            )
        return result

    def get_exercise(self, exercise_id: str) -> CodingExercise | None:
        with _get_db() as db:
            row = db.execute(
                "SELECT * FROM learning_exercises WHERE exercise_id = ?",
                (exercise_id,),
            ).fetchone()
        if not row:
            return None
        hints = []
        criteria = []
        rules = {}
        try:
            hints = json.loads(row["hints_json"] or "[]")
            criteria = json.loads(row["completion_criteria_json"] or "[]")
            rules = json.loads(row["validation_rules_json"] or "{}")
        except Exception:
            pass
        return CodingExercise(
            exercise_id=row["exercise_id"],
            space_id=row["space_id"],
            concept_id=row["concept_id"],
            canonical_issue_id=row["canonical_issue_id"],
            title=row["title"],
            description=row["description"] or "",
            instructions=row["instructions"] or "",
            language=row["language"],
            difficulty=row["difficulty"] or "beginner",
            category=row["category"] or "code_quality",
            starter_code=row["starter_code"] or "",
            buggy_code=row["buggy_code"],
            expected_behavior=row["expected_behavior"] or "",
            hints=hints,
            explanation=row["explanation"] or "",
            completion_criteria=criteria,
            validation_rules=rules,
            points=row["points"] or 10,
        )

    def save_draft(self, exercise_id: str, user_id: str, code: str) -> ExerciseDraft:
        draft_id = f"draft-{exercise_id}-{user_id}"
        now_str = datetime.now(UTC).isoformat()
        with _get_db() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO learning_drafts (draft_id, exercise_id, user_id, code, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (draft_id, exercise_id, user_id, code, now_str),
            )
        return ExerciseDraft(
            exercise_id=exercise_id, user_id=user_id, code=code, updated_at=datetime.fromisoformat(now_str)
        )

    def get_draft(self, exercise_id: str, user_id: str) -> ExerciseDraft | None:
        with _get_db() as db:
            row = db.execute(
                "SELECT * FROM learning_drafts WHERE exercise_id = ? AND user_id = ?",
                (exercise_id, user_id),
            ).fetchone()
        if not row:
            return None
        return ExerciseDraft(
            exercise_id=row["exercise_id"],
            user_id=row["user_id"],
            code=row["code"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def submit_exercise(self, exercise_id: str, user_id: str, code: str) -> ExerciseSubmission:
        exercise = self.get_exercise(exercise_id)
        if not exercise:
            raise ValueError("Exercise not found.")

        # Stage 6 is dropped: Perform truthful static evaluation without arbitrary code execution
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
            feedback_parts.append("Excellent! Static verification confirmed the vulnerable patterns were removed and defensive patterns were implemented.")
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

        sub_id = uuid4().hex
        now_str = datetime.now(UTC).isoformat()
        with _get_db() as db:
            db.execute(
                """
                INSERT INTO learning_submissions
                (submission_id, exercise_id, user_id, code, status, passed, feedback, eval_details_json, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sub_id, exercise_id, user_id, code, status, 1 if passed else 0, feedback, json.dumps(eval_details), now_str),
            )

            if passed:
                db.execute(
                    "UPDATE learning_concepts SET status = 'mastered' WHERE concept_id = ?",
                    (exercise.concept_id,),
                )
            else:
                db.execute(
                    "UPDATE learning_concepts SET status = 'in_progress' WHERE concept_id = ? AND status = 'not_started'",
                    (exercise.concept_id,),
                )

        return ExerciseSubmission(
            submission_id=sub_id,
            exercise_id=exercise_id,
            user_id=user_id,
            code=code,
            status=status,
            passed=passed,
            feedback=feedback,
            eval_details=eval_details,
            submitted_at=datetime.fromisoformat(now_str),
        )

    def list_submissions(self, exercise_id: str | None, user_id: str) -> list[ExerciseSubmission]:
        with _get_db() as db:
            if exercise_id:
                rows = db.execute(
                    "SELECT * FROM learning_submissions WHERE exercise_id = ? AND user_id = ? ORDER BY submitted_at DESC",
                    (exercise_id, user_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM learning_submissions WHERE user_id = ? ORDER BY submitted_at DESC LIMIT 50",
                    (user_id,),
                ).fetchall()
        result = []
        for r in rows:
            details = {}
            try:
                details = json.loads(r["eval_details_json"] or "{}")
            except Exception:
                pass
            result.append(
                ExerciseSubmission(
                    submission_id=r["submission_id"],
                    exercise_id=r["exercise_id"],
                    user_id=r["user_id"],
                    code=r["code"],
                    status=r["status"],
                    passed=bool(r["passed"]),
                    feedback=r["feedback"] or "",
                    eval_details=details,
                    submitted_at=datetime.fromisoformat(r["submitted_at"]),
                )
            )
        return result

    def get_dashboard(self, user_id: str | None = None) -> LearningLabDashboard:
        spaces = self.list_spaces(user_id)
        with _get_db() as db:
            concept_count = db.execute("SELECT COUNT(*) AS c FROM learning_concepts").fetchone()["c"]
            mastered_count = db.execute("SELECT COUNT(*) AS c FROM learning_concepts WHERE status = 'mastered'").fetchone()["c"]
            exercise_count = db.execute("SELECT COUNT(*) AS c FROM learning_exercises").fetchone()["c"]

            completed_subs = 0
            recent_subs = []
            if user_id:
                completed_subs = db.execute(
                    "SELECT COUNT(DISTINCT exercise_id) AS c FROM learning_submissions WHERE user_id = ? AND passed = 1",
                    (user_id,),
                ).fetchone()["c"]
                recent_subs = self.list_submissions(None, user_id)[:10]

        all_exercises = []
        for s in spaces[:5]:
            all_exercises.extend(self.list_exercises(s.space_id)[:2])

        return LearningLabDashboard(
            total_spaces=len(spaces),
            total_concepts=concept_count,
            mastered_concepts=mastered_count,
            total_exercises=exercise_count,
            completed_exercises=completed_subs,
            active_spaces=spaces[:10],
            recent_submissions=recent_subs,
            recommended_exercises=all_exercises[:6],
        )


_GLOBAL_SERVICE: LearningSpaceService | None = None


def get_learning_service() -> LearningSpaceService:
    global _GLOBAL_SERVICE
    if _GLOBAL_SERVICE is None:
        _GLOBAL_SERVICE = LearningSpaceService()
    return _GLOBAL_SERVICE
