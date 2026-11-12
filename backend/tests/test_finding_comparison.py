from app.verification.finding_comparison import compare_findings


def item(
    *,
    tool="ruff",
    path="src/a.py",
    rule="F821",
    message="undefined name",
    line=1,
    context="value",
    severity=None,
    confidence=None,
):
    return {
        "tool": tool,
        "file_path": path,
        "rule_id": rule,
        "message": message,
        "line": line,
        "column": 1,
        "end_line": line,
        "end_column": 4,
        "context": context,
        "severity": severity,
        "confidence": confidence,
    }


def test_identical_finding_moved_by_line_is_unchanged_and_relocated():
    result = compare_findings([item(line=2)], [item(line=8)])
    assert len(result) == 1
    assert result[0].category == "unchanged"
    assert result[0].relocated is True


def test_introduced_and_resolved_findings_are_separate():
    result = compare_findings([item(rule="F401")], [item(rule="F821")])
    assert {delta.category for delta in result} == {"introduced", "resolved"}


def test_unique_same_rule_and_message_with_changed_context_is_modified():
    result = compare_findings(
        [item(context="old source")], [item(context="new source")]
    )
    assert len(result) == 1
    assert result[0].category == "modified"


def test_unique_same_rule_with_changed_message_and_context_is_modified():
    result = compare_findings(
        [item(message="possibly unsafe call", context="eval(x)")],
        [item(message="unsafe call", context="eval(y)")],
    )
    assert len(result) == 1
    assert result[0].category == "modified"


def test_same_rule_multiplicity_with_changed_representations_is_uncertain():
    result = compare_findings(
        [
            item(message="possibly unsafe call", context="eval(x)"),
            item(message="unsafe operation", context="exec(x)"),
        ],
        [
            item(message="unsafe call", context="eval(y)"),
            item(message="unsafe operation changed", context="exec(y)"),
        ],
    )
    assert [delta.category for delta in result].count("uncertain") == 4
    assert all(delta.confidence == "low" for delta in result)


def test_different_tools_or_files_do_not_match():
    result = compare_findings(
        [item(tool="ruff", path="a.py")],
        [item(tool="bandit", path="b.py")],
    )
    assert {delta.category for delta in result} == {"introduced", "resolved"}


def test_exact_duplicates_are_multiset_matched_with_stable_unique_ids():
    baseline = [item(line=1), item(line=5), item(line=9)]
    proposed = [item(line=2), item(line=6)]
    result = compare_findings(baseline, proposed)
    assert [delta.category for delta in result].count("unchanged") == 2
    assert [delta.category for delta in result].count("resolved") == 1
    assert len({delta.delta_id for delta in result}) == len(result)


def test_ambiguous_duplicate_changes_are_uncertain_not_arbitrarily_paired():
    baseline = [item(context="old A"), item(context="old B")]
    proposed = [item(context="new A"), item(context="new B")]
    result = compare_findings(baseline, proposed)
    assert [delta.category for delta in result].count("uncertain") == 4
    assert len({delta.delta_id for delta in result}) == len(result)


def test_missing_rule_ids_only_match_exact_identity():
    baseline = [item(rule=None, message="warning", context="same")]
    proposed = [item(rule=None, message="warning", context="same", line=3)]
    exact = compare_findings(baseline, proposed)
    changed = compare_findings(
        baseline, [item(rule=None, message="other warning", context="different")]
    )
    assert exact[0].category == "unchanged"
    assert exact[0].relocated
    assert {delta.category for delta in changed} == {"uncertain"}


def test_bandit_severity_or_confidence_change_is_modified():
    result = compare_findings(
        [item(tool="bandit", severity="LOW", confidence="HIGH")],
        [item(tool="bandit", severity="HIGH", confidence="LOW")],
    )
    assert result[0].category == "modified"
