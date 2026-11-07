from app.verification.diff_mapping import added_lines_by_file


def test_maps_added_and_modified_lines_on_new_file_side():
    changed, limitations = added_lines_by_file([{
        "filename": "src/a.py", "status": "modified",
        "patch": "@@ -4,2 +4,3 @@\n same\n-old\n+new\n+another",
    }])
    assert changed == {"src/a.py": {5, 6}}
    assert limitations == {}


def test_added_file_and_zero_length_old_hunk_use_new_line_numbers():
    changed, limitations = added_lines_by_file([{
        "filename": "new.py", "status": "added", "patch": "@@ -0,0 +1,2 @@\n+first\n+second",
    }])
    assert changed == {"new.py": {1, 2}}
    assert limitations == {}


def test_added_source_lines_starting_with_diff_markers_are_counted():
    changed, limitations = added_lines_by_file([{
        "filename": "a.py", "status": "modified", "patch": "@@ -0,0 +1,2 @@\n+++value\n+--value",
    }])
    assert changed == {"a.py": {1, 2}}
    assert limitations == {}


def test_renamed_file_uses_current_path_and_deleted_file_is_not_analyzed():
    changed, limitations = added_lines_by_file([
        {"filename": "new_name.py", "previous_filename": "old_name.py", "status": "renamed", "patch": "@@ -1 +1 @@\n-old\n+new"},
        {"filename": "removed.py", "status": "removed", "patch": None},
        {"filename": "image.png", "status": "added", "patch": None},
    ])
    assert changed == {"new_name.py": {1}}
    assert limitations == {}


def test_context_only_lines_are_not_reported_as_pr_findings():
    changed, limitations = added_lines_by_file([{
        "filename": "src/a.py", "status": "modified", "patch": "@@ -8,2 +8,2 @@\n context\n-old\n+new",
    }])
    assert changed == {"src/a.py": {9}}
    assert 8 not in changed["src/a.py"]
    assert limitations == {}


def test_missing_or_malformed_diff_has_no_mappable_lines():
    changed, limitations = added_lines_by_file([
        {"filename": "missing.py", "status": "modified", "patch": None},
        {"filename": "bad.py", "status": "modified", "patch": "@@ -1 +1 @@\n+line\n context-without-prefix"},
    ])
    assert changed == {}
    assert set(limitations) == {"missing.py", "bad.py"}
    assert "did not provide" in limitations["missing.py"]
    assert "malformed" in limitations["bad.py"]


def test_truncated_diff_retains_known_lines_and_reports_incomplete_coverage():
    patch = "@@ -1 +1 @@\n-old\n+" + ("x" * 200_001)
    changed, limitations = added_lines_by_file([{"filename": "a.py", "status": "modified", "patch": patch}])
    assert changed == {"a.py": {1}}
    assert "truncated" in limitations["a.py"]


def test_malformed_hunk_line_counts_are_rejected():
    changed, limitations = added_lines_by_file([{
        "filename": "a.py", "status": "modified", "patch": "@@ -1,2 +1,2 @@\n-old\n+new",
    }])
    assert changed == {}
    assert "malformed" in limitations["a.py"]
