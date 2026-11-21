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


STANDARDIZED_TOPICS: dict[str, dict[str, Any]] = {
    "buffer_safety": {
        "title": "Safe String Copying with Buffer Bounds Checking",
        "real_world_analogy": (
            "Imagine pouring water from a large pitcher into a small glass. If you keep pouring without "
            "checking how tall the glass is, water spills everywhere onto the counter and ruins nearby papers. "
            "In programming, a 'buffer' is that glass. Unbounded functions like `strcpy()` keep writing until the source text "
            "ends, regardless of whether your destination buffer can hold it. Excess characters overwrite adjacent memory, "
            "causing crashes or giving attackers control over execution."
        ),
        "problem_explanation": (
            "The original code copies characters into `destination` without checking its capacity (`dest_size`). "
            "When `source` is larger than the destination buffer, the extra bytes overflow into neighboring stack or heap memory (CWE-120)."
        ),
        "mission": (
            "Refactor `safe_copy` to use a bounded copying function (like `snprintf` or `strncpy`) with `dest_size`. "
            "Ensure the destination string is always properly terminated with a null byte (`'\\0'`)."
        ),
        "example_input": "source = \"LongUserString...\", dest_size = 16",
        "example_output": "At most 15 characters copied into destination, ending with '\\0'",
        "hints": [
            "Hint 1: Consider how many bytes the destination buffer can actually hold before copying any characters.",
            "Hint 2: Use functions that take an explicit destination size bound instead of unbounded strcpy() or gets().",
            "Hint 3: Use `snprintf(destination, dest_size, \"%s\", source)` or `strncpy(destination, source, dest_size - 1)` with `destination[dest_size - 1] = '\\0'`.",
        ],
        "explanation": (
            "Bounded functions like `snprintf` and `strncpy` accept `dest_size`, enforcing an upper limit on memory writes. "
            "Explicitly guaranteeing a terminal null byte prevents read functions like `strlen` or `printf` from reading past the allocated buffer."
        ),
        "completion_criteria": [
            "Eliminate unbounded functions gets() and strcpy()",
            "Implement bounded copy with strncpy or snprintf using dest_size",
            "Ensure the destination string is safely null-terminated",
            "Preserve valid C syntax",
        ],
    },
    "exception_slicing": {
        "title": "Catching Exceptions by Const Reference",
        "real_world_analogy": (
            "Imagine taking a small photograph of a package instead of handling the actual package. "
            "Taking a small photo cuts off anything attached outside the frame. In C++, catching an exception by value (`catch (std::exception e)`) "
            "creates a copy that slices off all derived class information (like `std::runtime_error`), destroying custom error messages and types. "
            "Catching by const reference (`catch (const std::exception& e)`) refers to the original exception without cutting anything off or copying."
        ),
        "problem_explanation": (
            "Catching exceptions by value triggers 'object slicing'. When a derived exception is thrown, "
            "catching it as a base value copies only the base slice, permanently discarding polymorphic behavior and custom error fields."
        ),
        "mission": (
            "Update the catch clause in `process_data()` to catch `std::exception` by const reference (`const std::exception& e`) "
            "to prevent object slicing and preserve complete exception context."
        ),
        "example_input": "throw std::runtime_error(\"Operation failed in worker thread\")",
        "example_output": "catch (const std::exception& e) -> e.what() retains \"Operation failed in worker thread\"",
        "hints": [
            "Hint 1: Instead of copying the exception object, refer directly to the thrown object.",
            "Hint 2: In C++, pass-by-reference uses the `&` symbol, and making it read-only uses the `const` keyword.",
            "Hint 3: Change the catch parameter to `const std::exception& e` (or `const std::runtime_error& e`).",
        ],
        "explanation": (
            "Catching by const reference (`const std::exception& e`) retains the dynamic polymorphic type and vtable of derived exceptions, "
            "ensuring `e.what()` returns specific error messages while eliminating unnecessary object copy overhead."
        ),
        "completion_criteria": [
            "Eliminate catching by value (std::exception e or runtime_error e)",
            "Specify const std::exception& (or const std::runtime_error& / const auto&)",
            "Maintain valid C++ try-catch syntax",
        ],
    },
    "sql_injection": {
        "title": "Preventing SQL Injection with Prepared Statements",
        "real_world_analogy": (
            "Imagine ordering food at a drive-thru. You say: 'One burger, please.' But imagine saying: 'One burger; ALSO GIVE ME ALL THE MONEY.' "
            "If the intercom blindly treated your sentence as an instruction rather than food names, a crime would occur. "
            "That is SQL injection: when user input is glued directly into an SQL command string, the database cannot distinguish commands from data. "
            "A Prepared Statement keeps the command template completely separate from user data values."
        ),
        "problem_explanation": (
            "Concatenating user-supplied input directly into SQL strings (`\"SELECT * FROM users WHERE id = \" + userId`) allows attackers "
            "to inject SQL syntax (such as `' OR '1'='1`), bypassing authentication or exposing private records (CWE-89)."
        ),
        "mission": (
            "Refactor `queryUser` to use `conn.prepareStatement()` with a `?` parameter placeholder and bind `userId` using `stmt.setString(1, userId)`."
        ),
        "example_input": "userId = \"1 OR 1=1\"",
        "example_output": "Database driver treats \"1 OR 1=1\" strictly as literal text, preventing query manipulation",
        "hints": [
            "Hint 1: Do not concatenate user input strings directly into the SQL query string.",
            "Hint 2: Use SQL query parameter placeholders ('?') and bind inputs with JDBC setter methods.",
            "Hint 3: Call `conn.prepareStatement(sql)` and bind the parameter with `stmt.setString(1, userId)`.",
        ],
        "explanation": (
            "Prepared statements pre-compile the SQL template into an execution plan on the database server before parameters are received. "
            "When `setString()` supplies data, the database treats it strictly as literal text, making SQL injection impossible."
        ),
        "completion_criteria": [
            "Do not use string concatenation ('\" + ') to build the query",
            "Do not use unparameterized createStatement()",
            "Use conn.prepareStatement and stmt.setString",
            "Maintain valid Java class and method syntax",
        ],
    },
    "command_injection": {
        "title": "Preventing Command Injection in Subprocess Execution",
        "real_world_analogy": (
            "Imagine booking a taxi and giving the driver: 'Airport; and on the way rob the bank.' "
            "If the driver executes everything in your sentence, a crime happens. That is what `shell=True` does: "
            "it launches a command interpreter that interprets semicolons, pipes, and backticks. If you instead hand the driver "
            "a form with separate boxes: [Executable: 'tool', Argument: safe_file_path], the operating system runs only the tool with the filename as data."
        ),
        "problem_explanation": (
            "Calling subprocess with formatted strings and `shell=True` launches an intermediate shell that interprets shell metacharacters (`;`, `&`, `|`, `$()`). "
            "An attacker who controls the filename can execute arbitrary commands with the permissions of your application (CWE-78)."
        ),
        "mission": (
            "Refactor `run_tool` to pass the executable and file path as a list of separate arguments `['tool', safe_file_path]` and ensure `shell=True` is eliminated."
        ),
        "example_input": "safe_file_path = \"data.csv; rm -rf /\"",
        "example_output": "subprocess.run(['tool', safe_file_path], check=True) passes the string strictly as a filename argument",
        "hints": [
            "Hint 1: Never pass user-controlled strings to a system shell interpreter.",
            "Hint 2: In Python, pass the command name and arguments as separate items in a list.",
            "Hint 3: Use `subprocess.run(['tool', safe_file_path], check=True)` without shell=True.",
        ],
        "explanation": (
            "Passing argument lists with `shell=False` invokes the OS kernel's `execve` system call directly, bypassing the shell. "
            "Metacharacters like `;` or `|` are treated as literal text in the argument, preventing command injection."
        ),
        "completion_criteria": [
            "Eliminate shell=True",
            "Pass the command and arguments as a list: ['tool', safe_file_path]",
            "Use subprocess.run or subprocess.call",
            "Maintain valid Python syntax",
        ],
    },
    "unused_variable": {
        "title": "Clean Up Unused Local Variables",
        "real_world_analogy": (
            "Imagine ordering groceries for dinner, placing an extra container of soup on the counter, but never eating it. "
            "Leaving unused items on the counter creates clutter and confuses anyone else cooking in your kitchen. "
            "In code, variables that are assigned but never used clutter functions, mislead reviewers, and trigger linter warnings."
        ),
        "problem_explanation": (
            "A local variable was assigned but never read or referenced. "
            "Dead code clutters the codebase and can mask an incomplete implementation."
        ),
        "mission": "Remove the assignment to the unused variable while keeping the function's return calculation intact.",
        "example_input": "prices = [10.0, 20.0], tax_rate = 0.05",
        "example_output": "31.5 (clean calculation without unused local variable allocations)",
        "hints": [
            "Hint 1: Inspect the function to identify which variable is assigned but never referenced anywhere else.",
            "Hint 2: Delete the line defining `unused_variable`.",
            "Hint 3: Ensure `subtotal`, `total`, and `return total` remain intact.",
        ],
        "explanation": "Removing unused variables eliminates dead code, keeps functions focused, and satisfies static analysis linters (such as Ruff F841).",
        "completion_criteria": [
            "Remove unused variable assignment",
            "Calculate and return the correct total",
            "Ensure valid Python syntax",
        ],
    },
    "path_traversal": {
        "title": "Guarding File Access Against Path Traversal Escapes",
        "real_world_analogy": (
            "Imagine a hotel guest asking the front desk: 'Please fetch the document from Room 302.' "
            "If the guest writes: 'Room 302/../../ManagerOffice/Safe', following the '..' steps literally leads out of the room and into the manager's safe! "
            "In file systems, '..' moves up a directory. Path traversal happens when user input contains '..' to escape the repository folder and read sensitive system files."
        ),
        "problem_explanation": (
            "Combining a base path directly with user-supplied filenames allows attackers to supply relative parent segments like `../../etc/passwd` or symlinks that point outside the repository directory (CWE-22)."
        ),
        "mission": (
            "Resolve the target path canonically using `.resolve()` and verify it remains strictly within the checkout root using `is_relative_to()`."
        ),
        "example_input": "user_filename = \"../../../../etc/shadow\"",
        "example_output": "Target path resolved canonically and verified with is_relative_to(checkout_root.resolve())",
        "hints": [
            "Hint 1: Consider where the path actually points once relative segments ('..') and symbolic links are fully expanded.",
            "Hint 2: Simple string prefix checks (`str.startswith`) can be tricked by similarly named directories.",
            "Hint 3: Call `.resolve()` on the target path, then verify `target.is_relative_to(checkout_root.resolve())`.",
        ],
        "explanation": (
            "Canonical path resolution (`resolve()`) flattens all `..` segments and resolves symlinks. Checking `target.is_relative_to(root)` ensures the destination physically resides inside the designated directory."
        ),
        "completion_criteria": [
            "Resolve canonical target path with resolve()",
            "Verify path containment with is_relative_to()",
            "Prevent directory traversal escapes",
            "Maintain valid Python syntax",
        ],
    },
    "defensive_input": {
        "title": "Defensive Input Validation & Error Handling",
        "real_world_analogy": (
            "In software engineering, defensive validation is like checking the fuel gauge before driving: "
            "verifying your inputs before performing calculations prevents unexpected runtime crashes and ensures your system behaves reliably."
        ),
        "problem_explanation": (
            "Functions that assume inputs are always non-empty and well-formed can crash with unhandled exceptions when unexpected data arrives."
        ),
        "mission": (
            "Refactor the helper function to defensively validate incoming input and return a safe, sanitized result."
        ),
        "example_input": "value = None",
        "example_output": "\"\" (safe default returned without throwing an AttributeError)",
        "hints": [
            "Hint 1: Consider what happens when the function receives None, empty strings, or boundary values.",
            "Hint 2: Implement an explicit guard clause at the start of the function.",
            "Hint 3: Check `if not value: return \"\"` before calling methods like `.strip()`.",
        ],
        "explanation": (
            "Defensive programming guards against unexpected or malformed inputs at function boundaries, preventing unhandled exceptions and improving system stability."
        ),
        "completion_criteria": [
            "Implement defensive input validation guard",
            "Handle null and empty inputs safely",
            "Ensure valid syntax",
        ],
    },
}

STANDARDIZED_EXERCISE_CONTENT: dict[str, dict[str, Any]] = {
    "ex-concept-space-multi-project-5-1": STANDARDIZED_TOPICS["buffer_safety"],
    "ex-concept-space-sec-project-1-1": STANDARDIZED_TOPICS["buffer_safety"],
    "ex-concept-space-multi-project-5-2": STANDARDIZED_TOPICS["exception_slicing"],
    "ex-concept-space-multi-project-5-3": STANDARDIZED_TOPICS["sql_injection"],
    "ex-concept-space-multi-project-5-4": STANDARDIZED_TOPICS["command_injection"],
}


def _match_standard_topic(exercise: CodingExercise) -> dict[str, Any] | None:
    if exercise.exercise_id in STANDARDIZED_EXERCISE_CONTENT:
        return STANDARDIZED_EXERCISE_CONTENT[exercise.exercise_id]

    title_lower = exercise.title.lower()
    rules = exercise.validation_rules or {}
    forbidden = [str(f).lower() for f in rules.get("forbidden", [])]
    required = [str(r).lower() for r in (rules.get("required", []) + rules.get("required_any", []))]

    if "unused" in title_lower or "unused_variable =" in forbidden or any("unused" in f for f in forbidden):
        return STANDARDIZED_TOPICS["unused_variable"]
    if "safe string copying" in title_lower or "buffer" in title_lower or any("gets(" in f or "strcpy(" in f for f in forbidden):
        return STANDARDIZED_TOPICS["buffer_safety"]
    if "exception" in title_lower or "slicing" in title_lower or any("std::exception e" in f for f in forbidden):
        return STANDARDIZED_TOPICS["exception_slicing"]
    if "sql" in title_lower or any("createstatement" in f for f in forbidden) or any("preparestatement" in r for r in required):
        return STANDARDIZED_TOPICS["sql_injection"]
    if ("subprocess" in title_lower or "command injection" in title_lower) and any("shell=true" in f for f in forbidden):
        return STANDARDIZED_TOPICS["command_injection"]
    if "traversal" in title_lower or "is_relative_to" in required:
        return STANDARDIZED_TOPICS["path_traversal"]
    if "defensive input" in title_lower or "sanitize_input" in title_lower:
        return STANDARDIZED_TOPICS["defensive_input"]
    return None


def enrich_coding_exercise(exercise: CodingExercise) -> CodingExercise:
    """Enrich a coding exercise with beginner-first educational content standard."""
    std = _match_standard_topic(exercise)
    if std:
        data = exercise.model_dump()
        for key, val in std.items():
            data[key] = val
        return CodingExercise.model_validate(data)

    # If it already has educational fields populated, return as-is
    if exercise.real_world_analogy and exercise.problem_explanation and exercise.mission:
        return exercise

    # Otherwise synthesize beginner-first structure from existing metadata
    data = exercise.model_dump()
    desc = exercise.description or "Code quality opportunity identified in review."
    cat = exercise.category
    lang = exercise.language

    data["real_world_analogy"] = (
        f"In software development, writing clean and defensive {lang.upper()} code is like following safety protocols in a workshop: "
        f"addressing {cat.replace('_', ' ')} issues early prevents small oversight bugs from becoming major system failures."
    )
    data["problem_explanation"] = f"{desc} In production environments, this pattern reduces code maintainability and reliability."
    data["mission"] = exercise.instructions or "Refactor the starter code to implement the safe, defensive pattern."
    if not exercise.hints:
        data["hints"] = [
            f"Hint 1: Review the function structure and identify the problematic pattern in {lang.upper()}.",
            "Hint 2: Identify the recommended standard library or idiomatic pattern that resolves the issue.",
            "Hint 3: Align your solution with the completion criteria and test for syntax correctness.",
        ]
    if not exercise.completion_criteria:
        data["completion_criteria"] = [
            "Eliminate vulnerable or anti-pattern construct",
            "Implement defensive programming standard",
            "Ensure syntax validity",
        ]
    return CodingExercise.model_validate(data)


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
        desc = str(issue.get("normalized_description") or issue.get("description") or "").lower()
        cat = str(issue.get("category", "code_quality")).lower()
        if "unused" in desc or "variable" in desc:
            return "Dead Code Elimination & Unused Variables"
        if "traversal" in desc or ("path" in desc and any(w in desc for w in ("travers", "escape", "relative", "directory", ".."))):
            return "Path Traversal & Directory Containment"
        if "gets" in desc or "buffer" in desc or "strcpy" in desc:
            return "Buffer Safety & Bounds Checking"
        if "sql" in desc:
            return "SQL Injection & Query Parameterization"
        if "slicing" in desc or "exception" in desc:
            return "Safe Exception Handling & References"
        if "deserialization" in desc or "readobject" in desc:
            return "Secure Object Deserialization"
        if self._is_command_execution_issue(desc):
            return "Safe Subprocess & Argument Separation"
        if cat == "security":
            return f"Defensive Security Patterns ({lang.upper()})"
        return f"{cat.replace('_', ' ').title()} Principles ({lang.upper()})"

    @staticmethod
    def _is_command_execution_issue(desc_lower: str) -> bool:
        if not desc_lower:
            return False
        if (
            "command injection" in desc_lower
            or "command execution" in desc_lower
            or "unsafe command" in desc_lower
            or "external command" in desc_lower
            or "arbitrary command" in desc_lower
            or "os command" in desc_lower
        ):
            return True
        if (
            "shell" in desc_lower
            or "subprocess" in desc_lower
            or "popen" in desc_lower
            or "os.system" in desc_lower
        ):
            return True
        return False

    def _create_exercise_for_issue(
        self,
        exercise_id: str,
        space_id: str,
        concept_id: str,
        issue: dict[str, Any],
        lang: str,
    ) -> CodingExercise:
        desc = str(issue.get("normalized_description") or issue.get("description") or "").strip()
        desc_lower = desc.lower()
        file_path = str(issue.get("file_path", ""))
        cat = str(issue.get("category", "code_quality"))

        # 1. Unused variable / code quality cleanup pattern
        if "unused" in desc_lower or "variable" in desc_lower:
            title = "Clean Up Unused Local Variables"
            analogy = (
                "Imagine ordering groceries for dinner, placing an extra container of soup on the counter, but never eating it. "
                "Leaving unused items on the counter creates clutter and confuses anyone else cooking in your kitchen. "
                "In code, variables that are assigned but never used clutter functions, mislead reviewers, and trigger linter warnings."
            )
            problem = (
                f"Based on finding in {file_path}: a local variable was assigned but never read or referenced. "
                "Dead code clutters the codebase and can mask an incomplete implementation."
            )
            mission = "Remove the assignment to the unused variable while keeping the function's return calculation intact."
            instructions = "Refactor the function to remove `unused_variable`, returning the computed total cleanly."
            expected = "Function computes total and returns it without declaring or assigning unused local variables."
            starter = """def calculate_total(prices: list[float], tax_rate: float) -> float:
    unused_variable = "temporary debugging note"  # TODO: Remove unused variable
    subtotal = sum(prices)
    total = subtotal * (1 + tax_rate)
    return total
"""
            buggy = "unused_variable = \"temporary debugging note\""
            example_in = "prices = [10.0, 20.0], tax_rate = 0.05"
            example_out = "31.5 (clean calculation without unused local variable allocations)"
            hints = [
                "Hint 1: Inspect the function to identify which variable is assigned but never referenced anywhere else.",
                "Hint 2: Delete the line defining `unused_variable`.",
                "Hint 3: Ensure `subtotal`, `total`, and `return total` remain intact.",
            ]
            rules = {"forbidden": ["unused_variable ="], "required": ["return total", "subtotal"]}
            explanation = "Removing unused variables eliminates dead code, keeps functions focused, and satisfies static analysis linters."
            criteria = ["Remove unused variable assignment", "Calculate and return the correct total", "Ensure valid Python syntax"]
            difficulty = "beginner"
            effective_cat = "code_quality"

        # 2. Path traversal (when explicitly identified)
        elif "traversal" in desc_lower or ("path" in desc_lower and any(w in desc_lower for w in ("travers", "escape", "relative", "directory", ".."))):
            title = "Guarding File Access Against Path Traversal Escapes"
            analogy = (
                "Imagine a hotel guest asking the front desk: 'Please fetch the document from Room 302.' "
                "If the guest writes: 'Room 302/../../ManagerOffice/Safe', following the '..' steps literally leads out of the room and into the manager's safe! "
                "In file systems, '..' moves up a directory. Path traversal happens when user input contains '..' to escape the repository folder and read sensitive system files."
            )
            problem = f"Based on finding in {file_path}: {desc or 'Untrusted path handling could allow traversal escapes.'} Combining untrusted filenames with base directories allows path traversal attacks (CWE-22)."
            mission = "Resolve the target path canonically using `.resolve()` and verify it remains strictly within the checkout root using `is_relative_to()`."
            instructions = "Refactor the file access function to resolve the target path and verify it stays inside the allowed root directory."
            expected = "Target path resolved canonically and verified using is_relative_to."
            starter = """from pathlib import Path

def safe_read_file(checkout_root: Path, user_filename: str) -> str:
    # TODO: Resolve target path and verify it is relative to checkout_root.
    # Avoid reading files outside checkout_root.
    pass
"""
            buggy = "target = checkout_root / user_filename\nreturn target.read_text()"
            example_in = "user_filename = \"../../../../etc/shadow\""
            example_out = "Target path resolved canonically and verified with is_relative_to(checkout_root.resolve())"
            hints = [
                "Hint 1: Consider where the path actually points once relative segments ('..') and symbolic links are fully expanded.",
                "Hint 2: Simple string prefix checks (`str.startswith`) can be tricked by similarly named directories.",
                "Hint 3: Call `.resolve()` on the target path, then verify `target.is_relative_to(checkout_root.resolve())`.",
            ]
            rules = {"forbidden": ["checkout_root / user_filename\n    return", "checkout_root / user_filename\r\n    return"], "required": [".resolve()", "is_relative_to"]}
            explanation = "Canonical path resolution (`resolve()`) flattens all `..` segments and resolves symlinks. Checking `target.is_relative_to(root)` ensures the destination physically resides inside the designated directory."
            criteria = ["Resolve canonical target path with resolve()", "Verify path containment with is_relative_to()", "Prevent directory traversal escapes", "Maintain valid Python syntax"]
            difficulty = "intermediate"
            effective_cat = "security"

        # 3. C buffer bounds checking
        elif lang == "c" or "gets" in desc_lower or "buffer" in desc_lower or "strcpy" in desc_lower:
            title = "Safe String Copying with Buffer Bounds Checking"
            analogy = (
                "Imagine pouring water from a pitcher into a small glass. If you keep pouring without checking how tall "
                "the glass is, water spills everywhere onto the counter and ruins nearby papers. In C, an unbounded buffer "
                "write overflows into neighboring memory, causing crashes or security exploits."
            )
            problem = f"Based on finding in {file_path}: {desc or 'Unbounded string copy operations can overflow the destination buffer.'}"
            mission = "Refactor safe_copy to use a bounded copy function (like snprintf or strncpy) with dest_size and ensure null-termination."
            instructions = "Refactor the function to safely copy the string into destination bounded by dest_size, ensuring null-termination."
            expected = "Buffer bounds checked with strncpy or snprintf, preventing buffer overflow."
            starter = """#include <stdio.h>
#include <string.h>

void safe_copy(const char *source, char *destination, size_t dest_size) {
    // TODO: Safely copy source into destination without exceeding dest_size.
    // Avoid using unbounded gets() or strcpy().
    
}
"""
            buggy = "strcpy(destination, source);"
            example_in = "source = \"LongUserString...\", dest_size = 16"
            example_out = "At most 15 characters copied into destination, ending with '\\0'"
            hints = [
                "Hint 1: Consider how many bytes the destination buffer can actually hold before copying any characters.",
                "Hint 2: Use functions that take an explicit destination size bound instead of unbounded strcpy() or gets().",
                "Hint 3: Use `snprintf(destination, dest_size, \"%s\", source)` or `strncpy(destination, source, dest_size - 1)` with `destination[dest_size - 1] = '\\0'`.",
            ]
            rules = {"forbidden": ["gets(", "strcpy("], "required_any": ["strncpy", "snprintf"]}
            explanation = "Bounded functions enforce upper limits on memory writes, preventing buffer overflows (CWE-120)."
            criteria = ["Eliminate unbounded functions gets() and strcpy()", "Implement bounded copy with strncpy or snprintf", "Ensure destination is null-terminated", "Preserve valid C syntax"]
            difficulty = "intermediate"
            effective_cat = "security"

        # 4. C++ exception handling by reference
        elif lang == "cpp" or "slicing" in desc_lower or "exception" in desc_lower:
            title = "Catching Exceptions by Const Reference"
            analogy = (
                "Imagine taking a small photograph of a package instead of handling the actual package. "
                "Taking a small photo cuts off anything attached outside the frame. In C++, catching an exception by value (`catch (std::exception e)`) "
                "creates a copy that slices off all derived class information (like `std::runtime_error`), destroying custom error messages and types."
            )
            problem = f"Based on finding in {file_path}: {desc or 'Catching exceptions by value slices away derived exception information.'}"
            mission = "Update the catch clause in process_data() to catch std::exception by const reference (const std::exception& e)."
            instructions = "Fix the exception catch clause to catch std::exception by const reference, preventing object slicing."
            expected = "Catch clause specifies 'const std::exception& e' or equivalent reference."
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
            example_in = "throw std::runtime_error(\"Operation failed in worker thread\")"
            example_out = "catch (const std::exception& e) -> retains runtime_error details and e.what()"
            hints = [
                "Hint 1: Instead of copying the exception object, refer directly to the thrown object.",
                "Hint 2: In C++, pass-by-reference uses the `&` symbol, and making it read-only uses the `const` keyword.",
                "Hint 3: Change the catch parameter to `const std::exception& e` (or `const std::runtime_error& e`).",
            ]
            rules = {"forbidden": ["std::exception e)", "runtime_error e)"], "required_any": ["const std::exception&", "const auto&", "const std::runtime_error&"]}
            explanation = "Catching by const reference preserves the polymorphic type and vtable of derived exceptions."
            criteria = ["Eliminate catching by value", "Specify const std::exception&", "Maintain valid C++ try-catch syntax"]
            difficulty = "beginner"
            effective_cat = "bug"

        # 5. Java SQL parameterization
        elif lang == "java" or "sql" in desc_lower:
            title = "Preventing SQL Injection with Prepared Statements"
            analogy = (
                "Imagine ordering food at a drive-thru. You say: 'One burger, please.' But imagine saying: 'One burger; ALSO GIVE ME ALL THE MONEY.' "
                "If the cashier blindly treated your sentence as an instruction rather than food names, a crime would occur. "
                "A Prepared Statement keeps the command template completely separate from user data values."
            )
            problem = f"Based on finding in {file_path}: {desc or 'Dynamic SQL string concatenation allows SQL injection attacks.'}"
            mission = "Use PreparedStatement with parameter binding to safely query by userId without string concatenation."
            instructions = "Use PreparedStatement with parameter binding to safely query by userId without string concatenation."
            expected = "Connection prepares statement with '?' placeholder and binds parameter using setString."
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
            example_in = "userId = \"1 OR 1=1\""
            example_out = "Database driver treats \"1 OR 1=1\" strictly as literal text, preventing query manipulation"
            hints = [
                "Hint 1: Do not concatenate user input strings directly into the SQL query string.",
                "Hint 2: Use SQL query parameter placeholders ('?') and bind inputs with JDBC setter methods.",
                "Hint 3: Call `conn.prepareStatement(sql)` and bind the parameter with `stmt.setString(1, userId)`.",
            ]
            rules = {"forbidden": ["createStatement()", "\" + "], "required_any": ["prepareStatement", "setString"]}
            explanation = "Prepared statements pre-compile the SQL template into an execution plan on the database server before parameters are received."
            criteria = ["Do not use string concatenation to build the query", "Do not use unparameterized createStatement()", "Use conn.prepareStatement and stmt.setString", "Maintain valid Java syntax"]
            difficulty = "intermediate"
            effective_cat = "security"

        # 6. Command injection / Subprocess (ONLY when shell, subprocess, popen, os.system, or explicit command injection/execution is identified)
        elif self._is_command_execution_issue(desc_lower):
            title = "Preventing Command Injection in Subprocess Execution"
            analogy = (
                "Imagine booking a taxi and giving the driver: 'Airport; and on the way rob the bank.' "
                "If the driver executes everything in your sentence, a crime happens. That is what `shell=True` does: "
                "it launches a command interpreter that interprets semicolons, pipes, and backticks. If you instead hand the driver "
                "a form with separate boxes: [Executable: 'tool', Argument: safe_file_path], the operating system runs only the tool with the filename as data."
            )
            problem = f"Based on finding in {file_path}: {desc or 'Invoking external commands with shell=True allows command injection.'}"
            mission = "Replace the shell-interpolated command with an argument vector list using subprocess.run(..., check=True)."
            instructions = "Replace the shell-interpolated command with an argument vector list using subprocess.run(..., check=True)."
            expected = "Subprocess invoked with a list of arguments and shell=False."
            starter = """import subprocess
from pathlib import Path

def run_tool(safe_file_path: str) -> None:
    # TODO: Safely invoke the external tool without shell=True.
    # Pass the executable and file path as an argument list.
    
    pass
"""
            buggy = "subprocess.run(f'tool {safe_file_path}', shell=True)"
            example_in = "safe_file_path = \"data.csv; rm -rf /\""
            example_out = "subprocess.run(['tool', safe_file_path], check=True) passes the string strictly as a filename argument"
            hints = [
                "Hint 1: Never pass user-controlled strings to a system shell interpreter.",
                "Hint 2: In Python, pass the command name and arguments as separate items in a list.",
                "Hint 3: Use `subprocess.run(['tool', safe_file_path], check=True)` without shell=True.",
            ]
            rules = {"forbidden": ["shell=True", "shell = True"], "required_any": ["subprocess.run([", "subprocess.call(["]}
            explanation = "Passing argument lists with `shell=False` invokes the OS kernel's execve system call directly, bypassing the shell."
            criteria = ["Eliminate shell=True", "Pass command and arguments as a list: ['tool', safe_file_path]", "Use subprocess.run or subprocess.call", "Maintain valid Python syntax"]
            difficulty = "intermediate"
            effective_cat = "security"

        # 7. Safe neutral fallback for unknown, missing, empty, or unsupported issues
        else:
            title = "Defensive Input Validation & Error Handling"
            analogy = (
                "In software engineering, defensive validation is like checking the fuel gauge before driving: "
                "verifying your inputs before performing calculations prevents unexpected runtime crashes and ensures your system behaves reliably."
            )
            problem = f"Based on finding in {file_path or 'codebase'}: {desc or 'Code reliability or maintainability finding identified in code review.'} Functions should validate incoming data to prevent unexpected runtime errors."
            mission = "Refactor the function to validate incoming input defensively and return a valid result without raising uncaught exceptions."
            instructions = "Add defensive input checks to handle None or empty input safely before processing."
            expected = "Input checked defensively, handling edge cases without throwing unhandled exceptions."
            starter = """def sanitize_input(value: str | None) -> str:
    # TODO: Defensively guard against None or empty input.
    if not value:
        return ""
    return value.strip()
"""
            buggy = "return value.strip()  # Raises AttributeError if value is None"
            example_in = "value = None"
            example_out = "\"\" (safe default returned without throwing an AttributeError)"
            hints = [
                "Hint 1: Consider what happens when the function receives None, empty strings, or boundary values.",
                "Hint 2: Implement an explicit guard clause at the start of the function.",
                "Hint 3: Check `if not value: return \"\"` before calling methods like `.strip()`.",
            ]
            rules = {"forbidden": ["pass"], "required_any": ["if not value", "if value is None", "return \"\""]}
            explanation = "Defensive programming guards against unexpected or malformed inputs at function boundaries, preventing unhandled exceptions and improving system stability."
            criteria = ["Implement defensive input validation guard", "Handle null and empty inputs safely", "Ensure valid syntax"]
            difficulty = "beginner"
            effective_cat = cat if cat in ("bug", "security", "performance", "maintainability") else "code_quality"

        return CodingExercise(
            exercise_id=exercise_id,
            space_id=space_id,
            concept_id=concept_id,
            canonical_issue_id=issue.get("issue_id"),
            title=title,
            description=problem,
            instructions=instructions,
            language=lang if lang in ("python", "c", "cpp", "java") else "python",
            difficulty=difficulty,
            category=effective_cat if effective_cat in ("bug", "security", "performance", "maintainability") else "code_quality",
            starter_code=starter,
            buggy_code=buggy,
            expected_behavior=expected,
            hints=hints,
            explanation=explanation,
            completion_criteria=criteria,
            validation_rules=rules,
            real_world_analogy=analogy,
            problem_explanation=problem,
            mission=mission,
            example_input=example_in,
            example_output=example_out,
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
        exercises = self.store.list_exercises(space_id, concept_id)
        return [enrich_coding_exercise(e) for e in exercises]

    def get_exercise(self, exercise_id: str) -> CodingExercise | None:
        exercise = self.store.get_exercise(exercise_id)
        if exercise is None:
            return None
        return enrich_coding_exercise(exercise)

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
