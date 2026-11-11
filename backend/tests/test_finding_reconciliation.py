from app.schemas.finding import ReviewFinding
from app.schemas.issue import FindingObservation
from app.services.finding_reconciliation import reconcile_observations


def observation(
    *,
    finding_id="finding-1",
    source="static_analysis",
    file_path="src/query.py",
    line_start=20,
    line_end=20,
    category="security",
    severity="high",
    description="User input is interpolated into a SQL query.",
    evidence=None,
    rule_id="B608",
    tool_name="bandit",
    confidence=0.9,
):
    finding = ReviewFinding(
        id=finding_id,
        title=description,
        description=description,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        severity=severity,
        category=category,
        confidence=confidence,
        source=source,
        verification_status="needs_review",
        evidence=evidence or ["query = f'SELECT * FROM users WHERE id={user_id}'"],
        why_it_matters="Input may alter the query.",
        suggested_fix="Use a parameterized query.",
        learning_explanation="Keep query syntax separate from data.",
        limitations=[],
    )
    return FindingObservation(
        original_finding=finding,
        tool_name=tool_name,
        provider_name="example-provider" if source == "ai_candidate" else None,
        rule_id=rule_id,
    )


def test_identical_static_findings_group_and_have_stable_issue_id():
    first = observation(finding_id="ruff-1", tool_name="ruff", rule_id="S608")
    second = observation(finding_id="ruff-2", tool_name="ruff", rule_id="S608")

    result = reconcile_observations([first, second])
    reversed_result = reconcile_observations([second, first])

    assert result.unique_issue_count == 1
    assert result.observation_count == 2
    assert len(result.issues[0].observations) == 2
    assert result.issues[0].issue_id == reversed_result.issues[0].issue_id


def test_ruff_and_ai_duplicate_group_when_line_and_evidence_agree():
    ruff = observation(finding_id="ruff-1", tool_name="ruff", rule_id="S608")
    ai = observation(finding_id="ai-1", source="ai_candidate", rule_id=None, tool_name=None)

    result = reconcile_observations([ruff, ai])

    assert result.unique_issue_count == 1
    assert result.observation_count == 2
    assert {item.original_finding.source for item in result.issues[0].observations} == {"static_analysis", "ai_candidate"}


def test_bandit_and_ai_duplicate_group_with_rule_and_shared_evidence():
    bandit = observation(finding_id="bandit-1", tool_name="bandit", rule_id="B608")
    ai = observation(finding_id="ai-2", source="ai_candidate", rule_id=None, tool_name=None)

    result = reconcile_observations([bandit, ai])

    assert result.unique_issue_count == 1
    assert len(result.issues[0].observations) == 2
    assert any(item.rule_id == "B608" and item.tool_name == "bandit" for item in result.issues[0].observations)
    assert any(item.provider_name == "example-provider" for item in result.issues[0].observations)


def test_findings_from_different_files_never_merge():
    first = observation(finding_id="f1", file_path="src/a.py")
    second = observation(finding_id="f2", file_path="src/b.py")

    result = reconcile_observations([first, second])

    assert result.unique_issue_count == 2
    assert result.observation_count == 2


def test_different_nonempty_rule_ids_do_not_merge():
    first = observation(finding_id="f1", rule_id="B608")
    second = observation(finding_id="f2", rule_id="S608")

    result = reconcile_observations([first, second])

    assert result.unique_issue_count == 2


def test_nearby_but_unrelated_findings_do_not_merge():
    first = observation(finding_id="f1", line_start=20, line_end=20, description="SQL query interpolates user input.")
    second = observation(finding_id="f2", line_start=22, line_end=22, description="A file handle is not closed.", evidence=["open(path)"])

    result = reconcile_observations([first, second])

    assert result.unique_issue_count == 2


def test_missing_line_information_requires_same_rule_and_exact_message():
    first = observation(finding_id="f1", line_start=None, line_end=None, rule_id="B608")
    exact = observation(finding_id="f2", line_start=None, line_end=None, rule_id="B608")
    no_rule = observation(finding_id="f3", line_start=None, line_end=None, rule_id=None)

    matched = reconcile_observations([first, exact])
    uncertain = reconcile_observations([first, no_rule])

    assert matched.unique_issue_count == 1
    assert matched.observation_count == 2
    assert uncertain.unique_issue_count == 2


def test_missing_rule_id_needs_location_and_shared_evidence():
    first = observation(finding_id="f1", rule_id=None, source="static_analysis")
    corroborated = observation(finding_id="f2", rule_id=None, source="static_analysis")
    unrelated_evidence = observation(finding_id="f3", rule_id=None, source="static_analysis", evidence=["different code context"])

    matched = reconcile_observations([first, corroborated])
    unmerged = reconcile_observations([first, unrelated_evidence])

    assert matched.unique_issue_count == 1
    assert unmerged.unique_issue_count == 2


def test_one_canonical_issue_retains_every_observation_and_provenance():
    ruff = observation(finding_id="ruff-1", tool_name="ruff", rule_id="S608")
    bandit = observation(finding_id="bandit-1", tool_name="bandit", rule_id="S608")
    ai = observation(finding_id="ai-1", source="ai_candidate", rule_id=None, tool_name=None)

    result = reconcile_observations([ruff, bandit, ai])
    linked = result.issues[0].observations

    assert result.unique_issue_count == 1
    assert result.observation_count == 3
    assert {item.original_finding.id for item in linked} == {"ruff-1", "bandit-1", "ai-1"}
    assert {item.original_finding.source for item in linked} == {"static_analysis", "ai_candidate"}
    assert {item.tool_name for item in linked if item.tool_name} == {"ruff", "bandit"}
    assert any(item.provider_name == "example-provider" for item in linked)


def test_empty_input_returns_zero_counts_and_no_issues():
    result = reconcile_observations([])

    assert result.issues == []
    assert result.unique_issue_count == 0
    assert result.observation_count == 0


def test_unique_issue_count_is_distinct_from_raw_observation_count():
    duplicate_a = observation(finding_id="f1")
    duplicate_b = observation(finding_id="f2")
    separate = observation(finding_id="f3", line_start=40, line_end=40, description="A different vulnerability exists.", rule_id="B999", evidence=["other evidence"])

    result = reconcile_observations([duplicate_a, duplicate_b, separate])

    assert result.unique_issue_count == 2
    assert result.observation_count == 3
    assert sum(len(issue.observations) for issue in result.issues) == 3
