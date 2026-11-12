import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, test } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: './vite.config.ts',
  server: { middlewareMode: true },
  appType: 'custom',
});
const { default: ReviewWorkspacePage } = await vite.ssrLoadModule('/src/pages/ReviewWorkspacePage.tsx');
after(() => vite.close());

function finding(overrides = {}) {
  return {
    id: 'finding-ai-1', title: 'Unsafe input', description: 'Input reaches a shell command.',
    file_path: 'src/run.py', line_start: 12, line_end: 12, severity: 'high', category: 'security',
    confidence: 0.82, source: 'ai_candidate', verification_status: 'needs_review',
    evidence: ['subprocess.run(command, shell=True)'], why_it_matters: 'Untrusted input may execute commands.',
    suggested_fix: 'Pass an argument list.', learning_explanation: 'Shell parsing expands metacharacters.', limitations: [],
    ...overrides,
  };
}

function explanation(overrides = {}) {
  return {
    summary: 'Untrusted input can reach shell execution.',
    why_it_matters: 'An attacker could run commands with the application permissions.',
    root_cause: 'The shell parses user-controlled text as command syntax.',
    correction: 'Pass validated values as separate arguments.',
    code_example: { before: 'run(value, shell=True)', after: 'run(["tool", value], shell=False)', language: 'python' },
    concept: 'Keep data separate from executable command syntax.',
    learning_resources: [{ title: 'Python subprocess documentation', url: 'https://docs.python.org/3/library/subprocess.html', description: 'Process creation APIs.' }],
    caveats: ['The surrounding call sites were not available.'],
    ...overrides,
  };
}

function review(overrides = {}) {
  return {
    review_id: 'review-current', status: 'completed', analysis_mode: 'live', is_demo: false,
    pull_request: {
      repository_owner: 'acme', repository_name: 'widget', repository_url: 'https://github.com/acme/widget',
      pull_request_number: 42, pull_request_url: 'https://github.com/acme/widget/pull/42', title: 'Harden command handling',
      description: null, author: 'dev', base_branch: 'main', head_branch: 'feature', head_sha: null,
      state: 'open', changed_files: 2, additions: 8, deletions: 3,
    },
    summary: 'Review found a possible command injection.',
    findings: [finding()],
    canonical_issues: [{
      issue_id: 'issue-1', file_path: 'src/run.py', line_start: 12, line_end: 12, category: 'security', severity: 'high',
      normalized_description: 'Untrusted input reaches shell execution.', status: 'open',
      observations: [{ original_finding: finding(), tool_name: 'AI reviewer', provider_name: 'test-provider', rule_id: 'SEC-1' }],
      explanation: explanation(),
    }],
    unique_issue_count: 1, raw_observation_count: 1,
    observation_counts_by_source: { ai_candidate: 1 }, issue_counts_by_severity: { high: 1 }, issue_counts_by_category: { security: 1 },
    verification_summary: {}, tool_results: [
      { tool: 'tests', status: 'unavailable', executed: false, message: 'No test runner configured.' },
      { tool: 'issue_explanations', status: 'completed', issue_count: 1, explained_issue_count: 1, executed: true },
    ],
    warnings: [], created_at: '2026-11-01T12:00:00Z', duration_ms: 100,
    diff_files: [{ path: 'src/run.py', status: 'modified', added: 8, removed: 3, hunks: [], truncated: false }],
    ...overrides,
  };
}

function render(data, extra = {}) {
  return renderToStaticMarkup(React.createElement(ReviewWorkspacePage, {
    review: data, history: [], historyState: 'ready', historyError: '',
    diffFiles: data.diff_files ?? [], diffWarnings: [], onBackToHistory() {}, onSave() {}, saveMessage: '',
    onOpenDiff() {}, onOpenReview() {}, ...extra,
  }));
}

test('renders overview counts, canonical issue, provenance, evidence, explanation, validation and diff', () => {
  const html = render(review());
  for (const content of ['Review overview', 'UNIQUE ISSUES', '>1<', 'OBSERVATIONS', 'Findings',
    'Untrusted input reaches shell execution.', 'AI candidate', 'AI reviewer', 'SEC-1',
    'Original evidence and context', 'subprocess.run(command, shell=True)', 'AI explanations',
    'AI-generated guidance', 'Untrusted input can reach shell execution.', 'Root cause', 'Concept',
    'Python subprocess documentation', 'Limitations and uncertainty', 'PR tests', 'Not executed',
    'src/run.py', 'Open full diff', 'Review history']) {
    assert.ok(html.includes(content), `expected rendered workspace to include: ${content}`);
  }
});

test('exposes candidate generation and keeps generated tests unexecuted by default', () => {
  const data = review();
  data.canonical_issues[0].suggested_fixes = [{
    fix_id: 'fix-1', canonical_issue_id: 'issue-1', file_path: 'src/run.py',
    start_line: 12, end_line: 12, language: 'python', base_commit_sha: 'a'.repeat(40),
    expected_original_code: 'run(value, shell=True)', proposed_code: 'run([value], shell=False)',
    change_summary: 'Avoid shell parsing.', rationale: 'Separate data and commands.',
    limitations: [], confidence: 0.7, generation_status: 'generated', decision_status: 'pending',
    generated_at: '2026-01-01T00:00:00Z', validation_reports: [],
  }];
  const html = render(data, { accountId: 'user-1', onFixDecision: async () => {} });
  assert.ok(html.includes('Generate candidate tests'));
  assert.ok(html.includes('Run source validation'));
  assert.ok(!html.includes('Run in isolated worker'));
});

test('falls back to original findings for legacy reviews without canonical data', () => {
  const data = review({ canonical_issues: undefined, unique_issue_count: undefined, raw_observation_count: undefined });
  const html = render(data);
  assert.ok(html.includes('predates canonical issue grouping'));
  assert.ok(html.includes('Unsafe input'));
  assert.ok(html.includes('UNIQUE ISSUES'));
  assert.ok(html.includes('Not available for this older review'));
  assert.ok(html.includes('Guidance on this original finding'));
});

test('handles missing optional counts, findings, tools, warnings and diff metadata', () => {
  const data = review({
    findings: undefined, canonical_issues: [], unique_issue_count: 0, raw_observation_count: 0,
    observation_counts_by_source: undefined, issue_counts_by_severity: undefined,
    issue_counts_by_category: undefined, tool_results: undefined, warnings: undefined, diff_files: undefined,
    summary: '',
  });
  const html = render(data, { diffFiles: [] });
  assert.ok(html.includes('No issues returned'));
  assert.ok(html.includes('No explanation-generation status was recorded'));
  assert.ok(html.includes('No Ruff or Bandit status was returned.'));
  assert.ok(html.includes('Diff details unavailable'));
  assert.ok(html.includes('No counts available.'));
});

test('renders repository history loading and failure states accessibly', () => {
  const data = review();
  assert.ok(render(data, { historyState: 'loading' }).includes('role="status"'));
  assert.ok(render(data, { historyState: 'error', historyError: 'History request failed.' }).includes('History request failed.'));
});

test('one canonical explanation is attached to its issue even with multiple observations', () => {
  const data = review();
  data.canonical_issues[0].observations.push({
    original_finding: finding({ id: 'finding-ruff-2', source: 'static_analysis', title: 'Shell invocation' }),
    tool_name: 'Bandit', provider_name: null, rule_id: 'B602',
  });
  data.raw_observation_count = 2;
  const html = render(data);
  assert.equal((html.match(/AI-generated guidance/g) ?? []).length, 1);
  assert.equal((html.match(/workspace-observation"/g) ?? []).length, 2);
  assert.ok(html.indexOf('AI-generated guidance') > html.indexOf('issue-1'));
  assert.ok(html.includes('Bandit'));
  assert.ok(html.includes('B602'));
});

test('renders only explanation fields that exist and omits an absent code example', () => {
  const html = render(review({ canonical_issues: [{
    ...review().canonical_issues[0],
    explanation: { summary: 'A concise issue explanation.' },
  }] }));
  assert.ok(html.includes('A concise issue explanation.'));
  assert.ok(!html.includes('Root cause'));
  assert.ok(!html.includes('Example approach'));
  assert.ok(!html.includes('Before'));
  assert.ok(!html.includes('Further learning'));
  const empty = render(review({ canonical_issues: [{ ...review().canonical_issues[0], explanation: {} }] }));
  assert.ok(empty.includes('Explanation details were not included for this issue'));
  assert.ok(!empty.includes('AI-generated guidance'));
});

test('renders before and after code as escaped responsive code blocks', () => {
  const html = render(review({ canonical_issues: [{
    ...review().canonical_issues[0],
    explanation: explanation({ code_example: { before: '  run(value)\n', after: '  run([value])\n', language: 'python' } }),
  }] }));
  assert.ok(html.includes('>Before</h4>'));
  assert.ok(html.includes('>After</h4>'));
  assert.ok(html.includes('>python</span>'));
  assert.ok(html.includes('<pre><code>  run(value)'));
  assert.ok(html.includes('<pre><code>  run([value])'));
  assert.ok(html.includes('workspace-code-grid'));
});

test('learning resources use safe external-link behavior and reject untrusted hosts', () => {
  const safeHtml = render(review());
  assert.ok(safeHtml.includes('href="https://docs.python.org/3/library/subprocess.html" target="_blank" rel="noopener noreferrer"'));
  const unsafeHtml = render(review({ canonical_issues: [{
    ...review().canonical_issues[0],
    explanation: explanation({ learning_resources: [{ title: 'Unexpected destination', url: 'https://attacker.example/guide' }] }),
  }] }));
  assert.ok(unsafeHtml.includes('Unexpected destination <small>(link unavailable)</small>'));
  assert.ok(!unsafeHtml.includes('href="https://attacker.example/guide"'));
});

test('shows caveats and clearly labels AI guidance as unverified', () => {
  const html = render(review());
  assert.ok(html.includes('The surrounding call sites were not available.'));
  assert.ok(html.includes('Educational interpretation; not a verified fix'));
});

test('represents partial, unavailable, not-requested, and missing explanation statuses accurately', () => {
  const issueWithoutExplanation = [{ ...review().canonical_issues[0], explanation: null }];
  const partial = render(review({ canonical_issues: issueWithoutExplanation, tool_results: [{ tool: 'issue_explanations', status: 'partial', issue_count: 2, explained_issue_count: 1 }] }));
  assert.ok(partial.includes('Explanation generation partially completed'));
  assert.ok(partial.includes('No explanation was saved for this issue'));
  const unavailable = render(review({ canonical_issues: issueWithoutExplanation, tool_results: [{ tool: 'issue_explanations', status: 'unavailable', executed: false }] }));
  assert.ok(unavailable.includes('Explanation generation was unavailable'));
  assert.ok(unavailable.includes('This issue and its original evidence are preserved'));
  const failed = render(review({ canonical_issues: issueWithoutExplanation, tool_results: [{ tool: 'issue_explanations', status: 'unavailable', executed: true }] }));
  assert.ok(failed.includes('Explanation generation failed'));
  assert.ok(failed.includes('This issue and its original evidence are preserved'));
  const notRequested = render(review({ canonical_issues: issueWithoutExplanation, tool_results: [{ tool: 'issue_explanations', status: 'not_requested' }] }));
  assert.ok(notRequested.includes('Explanations were not requested for this review'));
  const noStatus = render(review({ canonical_issues: issueWithoutExplanation, tool_results: [{ tool: 'tests', status: 'completed' }] }));
  assert.ok(noStatus.includes('No explanation-generation status was recorded'));
  assert.ok(noStatus.includes('No explanation is stored for this issue'));
  assert.ok(!noStatus.includes('No problems were found'));
});

test('preserves legacy finding guidance without inventing canonical explanations or status', () => {
  const html = render(review({ canonical_issues: undefined, unique_issue_count: undefined, tool_results: [] }));
  assert.ok(html.includes('legacy review has no canonical explanation status'));
  assert.ok(html.includes('Guidance on this original finding'));
  assert.ok(html.includes('Shell parsing expands metacharacters.'));
  assert.ok(!html.includes('AI-generated educational explanation'));
});

test('keeps issue and evidence disclosure keyboard-accessible and responsive code containers present', () => {
  const html = render(review());
  assert.ok(html.includes('<details class="workspace-issue">'));
  assert.ok(html.includes('<summary>'));
  assert.ok(html.includes('<details class="workspace-evidence">'));
  assert.ok(html.includes('aria-label="AI-generated educational explanation"'));
  assert.ok(html.includes('role="status"'));
});

test('uses a narrow-screen code layout in the workspace styles', async () => {
  const styles = await readFile(new URL('../styles.css', import.meta.url), 'utf8');
  assert.match(styles, /@media\s*\(max-width:\s*620px\)[\s\S]*?\.workspace-code-grid\s*\{\s*grid-template-columns:\s*1fr;/);
  assert.match(styles, /\.workspace-code-grid pre\s*\{[^}]*overflow-x:\s*auto/s);
});
