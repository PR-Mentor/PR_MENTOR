"""Curated exercises. Answers and explanations are only returned after submission."""

EXERCISES = [
    {
        "id": "empty-average-1",
        "title": "Guarding Against Division by Zero on Empty Collections",
        "description": "A helper calculates an average from caller-provided numbers. Learn how to prevent division by zero using an explicit guard check.",
        "language": "python",
        "difficulty": "beginner",
        "category": "bug",
        "real_world_analogy": (
            "Imagine a teacher calculating a class test average. If 20 students took the test, "
            "the teacher adds all 20 scores and divides by 20. But what happens on a snow day when 0 students show up? "
            "You cannot divide by zero! In programming, if a list is empty, its length is 0. Attempting to divide by 0 "
            "crashes the program. Checking if the list is empty first is like checking whether any students showed up before doing math."
        ),
        "problem_explanation": (
            "The original function calculates `sum(values) / len(values)` directly. When `values` is an empty list `[]`, "
            "`len(values)` evaluates to 0, raising an uncaught `ZeroDivisionError` that crashes the application."
        ),
        "mission": "Identify the defensive pattern that explicitly handles empty collections before attempting any division.",
        "instructions": "Which change prevents division by zero while keeping the empty case explicit?",
        "code_snippet": "def average(values: list[float]) -> float:\n    return sum(values) / len(values)",
        "example_input": "values = []",
        "example_output": "0.0 (prevents ZeroDivisionError crash)",
        "hints": [
            "Hint 1: What is the value of len(values) when values contains no elements?",
            "Hint 2: An explicit guard check at the beginning of a function handles edge cases cleanly before the main calculation.",
            "Hint 3: In Python, empty collections are falsy (`if not values: return 0.0`), or check `if len(values) == 0`.",
        ],
        "options": [
            "Return 0 when values is empty, otherwise calculate the average.",
            "Catch every Exception around the division.",
            "Convert values to a tuple before dividing.",
        ],
        "learning_objectives": [
            "Identify empty collection edge cases",
            "Prevent ZeroDivisionError using explicit guard clauses",
            "Establish predictable function return contracts",
        ],
        "correct_answer": "Return 0 when values is empty, otherwise calculate the average.",
        "explanation": (
            "An explicit guard check (`if not values: return 0`) prevents division by zero before it can occur, "
            "establishing a documented, reliable contract for callers without relying on slow or broad exception catching."
        ),
    },
    {
        "id": "shell-input-1",
        "title": "Preventing Shell Injection with Subprocess Argument Lists",
        "description": "A program needs to call a local analyzer with a user-selected file path. Learn why shell parsing must be disabled.",
        "language": "python",
        "difficulty": "intermediate",
        "category": "security",
        "real_world_analogy": (
            "Imagine booking a taxi and telling the driver: 'Take me to Airport; and on the way rob the bank.' "
            "If the driver blindly executes every command in your sentence, a crime happens. That is what `shell=True` does: "
            "it launches a command interpreter that obeys semicolons, pipes, and backticks. If you instead hand the driver "
            "a form with a single box labeled 'Destination: Airport', the driver only drives to the airport."
        ),
        "problem_explanation": (
            "When external commands are formatted as strings with `shell=True`, special shell characters (`;`, `|`, `&`, `$()`) "
            "in user input are interpreted as new commands, enabling arbitrary command injection attacks."
        ),
        "mission": "Choose the subprocess invocation that passes arguments as a list and disables the shell interpreter.",
        "instructions": "Which subprocess call avoids interpreting the file path as shell syntax?",
        "code_snippet": "path = request.query_params['path']\n# Run the analyzer on path",
        "example_input": "path = 'sample.txt; rm -rf /'",
        "example_output": "['analyzer', 'sample.txt; rm -rf /'] treated as a literal filename argument, not shell commands",
        "hints": [
            "Hint 1: Avoid letting the system shell parse user input strings.",
            "Hint 2: Passing an argument list (vector) tells the OS kernel to treat each list element strictly as data.",
            "Hint 3: Use `subprocess.run(['analyzer', path], shell=False, check=False)` to prevent shell command parsing.",
        ],
        "options": [
            "subprocess.run(['analyzer', path], shell=False, check=False)",
            "subprocess.run(f'analyzer {path}', shell=True)",
            "eval(path)",
        ],
        "learning_objectives": [
            "Recognize command injection risks in subprocess calls",
            "Use argument vectors (lists) instead of formatted shell strings",
            "Enforce shell=False to bypass shell metacharacter parsing",
        ],
        "correct_answer": "subprocess.run(['analyzer', path], shell=False, check=False)",
        "explanation": (
            "Passing an argument list with `shell=False` invokes the executable directly via OS system calls (like `execve`), "
            "ensuring metacharacters in the filename are treated strictly as literal characters rather than shell syntax."
        ),
    },
    {
        "id": "path-traversal-1",
        "title": "Guarding File Access Against Path Traversal Escapes",
        "description": "A review service reads a file named by a pull-request diff. Learn how to prevent directory traversal and symlink escapes.",
        "language": "python",
        "difficulty": "intermediate",
        "category": "security",
        "real_world_analogy": (
            "Imagine a hotel guest asking the front desk: 'Please fetch the document from Room 302.' "
            "If the guest writes: 'Room 302/../../ManagerOffice/Safe', and the clerk follows the '..' (go up one floor) steps literally, "
            "the clerk walks right out of Room 302 and into the manager's safe! In file systems, '..' moves up a directory. "
            "Path traversal happens when user input contains '..' to escape the repository folder and read sensitive system files."
        ),
        "problem_explanation": (
            "Directly combining a base path with user-supplied filename (`checkout / filename`) allows attackers to supply "
            "relative parent segments like `../../etc/passwd` or symlinks that point outside the repository directory."
        ),
        "mission": "Identify the path validation technique that resolves the canonical path and verifies it remains confined within the checkout root.",
        "instructions": "What must be checked before opening the file?",
        "code_snippet": "target = checkout / filename\ntext = target.read_text()",
        "example_input": "filename = '../../../../etc/shadow'",
        "example_output": "Target resolved canonical path verified with is_relative_to(checkout.resolve())",
        "hints": [
            "Hint 1: Consider where the path actually points once relative segments ('..') and symbolic links are fully expanded.",
            "Hint 2: Simple string prefix checks (`str.startswith`) can be tricked by similarly named directories or un-resolved dots.",
            "Hint 3: Call `.resolve()` on the target path, then verify `resolved.is_relative_to(checkout.resolve())`.",
        ],
        "options": [
            "Resolve the target and verify it remains beneath the checkout root.",
            "Check that the string starts with the checkout path.",
            "Replace every dot in the filename.",
        ],
        "learning_objectives": [
            "Understand directory traversal vulnerabilities (CWE-22)",
            "Resolve canonical paths to eliminate dots and symlinks",
            "Confine file access strictly beneath the designated root directory",
        ],
        "correct_answer": "Resolve the target and verify it remains beneath the checkout root.",
        "explanation": (
            "Canonical path resolution (`resolve()`) flattens all `..` segments and resolves symlinks. Checking "
            "`resolved.is_relative_to(root)` ensures the destination physically resides inside the designated directory."
        ),
    },
]
