import {
  Activity, ArrowRight, CircleAlert, ExternalLink, FileCode2, GitPullRequest,
  History, ShieldCheck, Sparkles,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { cancelTestExecution, enqueueTestExecution, generateTestsForFix, getRepositoryIntelligence, getTestExecutionJob, requestSemanticReview, validateSuggestedFix } from '../services/api';
import type { CanonicalIssue, DiffFile, FixValidationReport, GeneratedTestCase, IssueExplanation, LearningResource, RepositoryIntelligence, ReviewFinding, ReviewResponse, SuggestedFix, TestExecutionJob, ToolResult } from '../types/review';

type HistoryState = 'idle' | 'loading' | 'error' | 'ready';

interface Props {
  accountId?: string;
  review: ReviewResponse;
  history: ReviewResponse[];
  historyState: HistoryState;
  historyError: string;
  diffFiles: DiffFile[];
  diffWarnings: string[];
  onBackToHistory: () => void;
  onSave: () => void;
  saveMessage: string;
  onOpenDiff: () => void;
  onOpenReview: (reviewId: string) => void;
  onFixDecision: (issueId: string, fixId: string, decision: 'accepted' | 'rejected') => Promise<void>;
}

const sectionLinks = [
  ['overview', 'Overview'], ['findings', 'Findings'], ['explanations', 'AI explanations'],
  ['validation', 'Validation'], ['repository-context', 'Repository context'], ['diff', 'Code & diff'], ['history', 'History'],
] as const;

export default function ReviewWorkspacePage({
  accountId = '', review, history, historyState, historyError, diffFiles, diffWarnings,
  onBackToHistory, onSave, saveMessage, onOpenDiff, onOpenReview, onFixDecision,
}: Props) {
  const pullRequest = review.pull_request;
  const issueData = getIssueData(review);
  const distributionItems = issueData.mode === 'canonical' ? issueData.issues : issueData.legacyFindings;
  const sourceCounts = review.observation_counts_by_source ?? countSources(issueData);
  const severityCounts = review.issue_counts_by_severity ?? countBy<CanonicalIssue | ReviewFinding>(distributionItems, (item) => item.severity);
  const categoryCounts = review.issue_counts_by_category ?? countBy<CanonicalIssue | ReviewFinding>(distributionItems, (item) => item.category);
  const previousReviews = history.filter((item) =>
    item.review_id !== review.review_id
    && item.pull_request.repository_owner === pullRequest.repository_owner
    && item.pull_request.repository_name === pullRequest.repository_name,
  );
  const uniqueCount = review.unique_issue_count ?? (issueData.mode === 'canonical' ? issueData.issues.length : null);
  const observationCount = review.raw_observation_count ?? review.findings?.length ?? 0;
  const tools = review.tool_results ?? [];

  return <section className="content-page review-workspace">
    <div className="workspace-back-row">
      <button type="button" className="workspace-back" onClick={onBackToHistory}><History size={15}/> Recent reviews</button>
      <span className={`mode-badge ${review.status === 'partial' ? 'demo' : 'live'}`}>{review.status === 'partial' ? 'PARTIAL REVIEW' : 'REVIEW COMPLETE'}</span>
    </div>

    <header className="workspace-header">
      <div className="workspace-title-block">
        <div className="eyebrow">PULL REQUEST REVIEW <span>#{pullRequest.pull_request_number}</span></div>
        <h1>{pullRequest.title || `Pull request #${pullRequest.pull_request_number}`}</h1>
        <a className="workspace-repository" href={pullRequest.pull_request_url} target="_blank" rel="noreferrer">
          <GitPullRequest size={15}/>{pullRequest.repository_owner}/{pullRequest.repository_name}<ExternalLink size={13}/>
        </a>
      </div>
      <div className="workspace-header-meta">
        <span className="workspace-pr-state">{pullRequest.state || 'State unavailable'}</span>
        <span>{pullRequest.author ? `opened by ${pullRequest.author}` : 'Author unavailable'}</span>
        <span>{pullRequest.head_branch || 'head'} <ArrowRight size={12}/> {pullRequest.base_branch || 'base'}</span>
      </div>
      <div className="workspace-header-actions"><button type="button" className="secondary-button" onClick={onSave}>Save pull request</button>{saveMessage && <span role="status">{saveMessage}</span>}</div>
    </header>

    {review.is_demo && <div className="workspace-notice demo-notice"><Sparkles size={16}/><span>Illustrative demo data only. These issues do not describe the submitted pull request.</span></div>}
    {review.warnings?.length > 0 && <div className="workspace-warnings" aria-label="Review warnings">
      {review.warnings.map((warning, index) => <div key={`${index}-${warning}`}><CircleAlert size={14}/>{warning}</div>)}
    </div>}

    <nav className="workspace-nav" aria-label="Review sections">
      {sectionLinks.map(([id, label]) => <a key={id} href={`#${id}`}>{label}</a>)}
    </nav>

    <section className="workspace-section workspace-overview" id="overview" aria-labelledby="overview-title">
      <div className="workspace-section-heading"><div><span className="section-index">01</span><div><h2 id="overview-title">Review overview</h2><p>{review.summary}</p></div></div></div>
      <div className="workspace-metrics">
        <Metric label="UNIQUE ISSUES" value={uniqueCount === null ? '—' : String(uniqueCount)} detail={issueData.mode === 'legacy' ? 'Not available for this older review' : 'Canonical review issues'}/>
        <Metric label="OBSERVATIONS" value={String(observationCount)} detail="Source findings retained as evidence"/>
        <Metric label="CHANGED FILES" value={String(pullRequest.changed_files)} detail={`${pullRequest.additions} additions · ${pullRequest.deletions} deletions`}/>
        <Metric label="ANALYSIS" value={review.analysis_mode === 'live' ? 'Live' : 'Demo'} detail={`Created ${formatDate(review.created_at)}`}/>
      </div>
      <div className="workspace-overview-grid">
        <Distribution title={issueData.mode === 'legacy' ? 'Finding severity' : 'Issue severity'} counts={severityCounts}/>
        <Distribution title={issueData.mode === 'legacy' ? 'Finding categories' : 'Issue categories'} counts={categoryCounts}/>
        <Distribution title="Observation sources" counts={sourceCounts}/>
      </div>
    </section>

    <section className="workspace-section" id="findings" aria-labelledby="findings-title">
      <div className="workspace-section-heading"><div><span className="section-index">02</span><div><h2 id="findings-title">Findings</h2><p>{issueData.mode === 'canonical' ? 'Canonical issues group evidence from each reporting source.' : 'This stored review predates canonical issue grouping; original findings are shown as received.'}</p></div></div><span className="workspace-count">{issueData.mode === 'canonical' ? `${issueData.issues.length} ISSUES` : `${issueData.legacyFindings.length} FINDINGS`}</span></div>
      {issueData.mode === 'canonical'
        ? issueData.issues.length
          ? <div className="workspace-issue-list">{issueData.issues.map((issue) => <CanonicalIssueCard issue={issue} review={review} accountId={accountId} onFixDecision={onFixDecision} key={issue.issue_id}/>)}</div>
          : <EmptyNotice title="No issues returned" detail="No canonical issues were reported for this review. Check validation status and warnings before treating this as a clean review."/>
        : issueData.legacyFindings.length
          ? <div className="workspace-issue-list">{issueData.legacyFindings.map((finding) => <LegacyFindingCard finding={finding} key={finding.id}/>)}</div>
          : <EmptyNotice title="No findings returned" detail="This review contains no findings. Review the tool statuses and warnings for analysis coverage."/>}
    </section>

    <section className="workspace-section" id="explanations" aria-labelledby="explanations-title">
      <div className="workspace-section-heading"><div><span className="section-index">03</span><div><h2 id="explanations-title">AI explanations</h2><p>Educational guidance belongs to each canonical issue. Original tool and AI observations remain separate evidence.</p></div></div></div>
      <ExplanationGenerationStatus review={review} legacy={issueData.mode === 'legacy'}/>
      {issueData.mode === 'legacy' && issueData.legacyFindings.some(hasLegacyExplanation)
        ? <p className="workspace-muted">This older review has no canonical explanations. Existing AI candidate guidance is shown with its original finding.</p>
        : null}
    </section>

    <section className="workspace-section" id="validation" aria-labelledby="validation-title">
      <div className="workspace-section-heading"><div><span className="section-index">04</span><div><h2 id="validation-title">Validation & analysis</h2><p>Reported tool status and execution evidence from this review.</p></div></div></div>
      <div className="workspace-validation-grid">
        <ToolGroup title="Static analysis" tools={tools.filter((tool) => ['ruff', 'bandit'].includes(tool.tool))} empty="No Ruff or Bandit status was returned."/>
        <ToolGroup title="AI review" tools={tools.filter((tool) => tool.tool === 'ai_review')} empty="No AI review status was returned."/>
        <ToolGroup title="PR tests" tools={tools.filter((tool) => tool.tool === 'tests')} empty="No PR test-runner status was returned."/>
      </div>
      <p className="workspace-validation-note"><ShieldCheck size={14}/> Tool status is shown as reported. PR tests are not implied unless the backend marks them executed.</p>
    </section>

    <section className="workspace-section" id="repository-context" aria-labelledby="repository-context-title">
      <div className="workspace-section-heading"><div><span className="section-index">05</span><div><h2 id="repository-context-title">Repository context</h2><p>Snapshot-bound Python structure and import relationships for the public repository.</p></div></div></div>
      {!review.is_demo && pullRequest.head_sha
        ? <RepositoryIntelligencePanel owner={pullRequest.repository_owner} repository={pullRequest.repository_name} refSha={pullRequest.head_sha}/>
        : <p className="workspace-muted">Repository intelligence is available for live reviews with a full commit SHA.</p>}
    </section>

    <section className="workspace-section" id="diff" aria-labelledby="diff-title">
      <div className="workspace-section-heading"><div><span className="section-index">05</span><div><h2 id="diff-title">Code & diff</h2><p>Inspect the changed files and open the existing full diff view.</p></div></div><button type="button" className="secondary-button workspace-open-diff" onClick={onOpenDiff}>Open full diff <ArrowRight size={14}/></button></div>
      {diffFiles.length
        ? <div className="workspace-file-list">{diffFiles.slice(0, 8).map((file) => <div key={`${file.path}-${file.status}`}><FileCode2 size={15}/><code>{file.path}</code><span>{file.added ? `+${file.added}` : ''}{file.removed ? ` −${file.removed}` : ''}</span><em>{pretty(file.status)}</em></div>)}</div>
        : <EmptyNotice title="Diff details unavailable" detail="No diff file data was included in this review response. The full diff endpoint may still contain stored patch data."/>}
      {diffWarnings.map((warning, index) => <p className="workspace-diff-warning" key={`${index}-${warning}`}><CircleAlert size={13}/>{warning}</p>)}
    </section>

    <section className="workspace-section" id="history" aria-labelledby="history-title">
      <div className="workspace-section-heading"><div><span className="section-index">06</span><div><h2 id="history-title">Review history</h2><p>Other stored reviews for this repository.</p></div></div><button type="button" className="text-action" onClick={onBackToHistory}>All reviews <ArrowRight size={13}/></button></div>
      {historyState === 'loading' && <div className="workspace-inline-state" role="status">Loading repository review history…</div>}
      {historyState === 'error' && <div className="workspace-inline-state" role="alert">{historyError || 'Review history is unavailable.'}</div>}
      {historyState === 'ready' && (previousReviews.length
        ? <div className="workspace-history-list">{previousReviews.slice(0, 5).map((item) => <button key={item.review_id} type="button" onClick={() => onOpenReview(item.review_id)}><span><strong>{item.pull_request.title || `Pull request #${item.pull_request.pull_request_number}`}</strong><small>#{item.pull_request.pull_request_number} · {formatDate(item.created_at)}</small></span><span className="workspace-count">{typeof item.unique_issue_count === 'number' ? `${item.unique_issue_count} issues` : 'Open review'}</span><ArrowRight size={15}/></button>)}</div>
        : <EmptyNotice title="No earlier repository reviews" detail="History appears here when this repository has other stored reviews."/>)}
    </section>
  </section>;
}

function RepositoryIntelligencePanel({ owner, repository, refSha }: { owner: string; repository: string; refSha: string }) {
  const [state, setState] = useState<'idle' | 'loading' | 'error' | 'ready'>('idle');
  const [data, setData] = useState<RepositoryIntelligence | null>(null);
  const [error, setError] = useState('');
  const load = async () => {
    setState('loading'); setError('');
    try { setData(await getRepositoryIntelligence(owner, repository, refSha)); setState('ready'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Repository context is unavailable.'); setState('error'); }
  };
  if (state === 'idle') return <button className="secondary-button" type="button" onClick={() => void load()}>Analyze repository structure</button>;
  if (state === 'loading') return <p role="status">Scanning bounded repository metadata at this review snapshot…</p>;
  if (state === 'error') return <div role="alert" className="workspace-fix-error">{error} <button type="button" onClick={() => void load()}>Retry</button></div>;
  if (!data) return null;
  return <div className="repository-intelligence"><div className="analytics-cards"><AnalyticsValue label="FILES IN MANIFEST" value={String(data.manifest_count)} detail={data.manifest_truncated ? 'Inventory was capped' : 'Snapshot inventory'}/><AnalyticsValue label="PYTHON MODULES" value={String(data.python_modules.length)} detail={`${data.python_files_considered} bounded source files considered`}/><AnalyticsValue label="IMPORT LINKS" value={String(data.internal_import_edges.length)} detail="Internal module relationships"/><AnalyticsValue label="CYCLES" value={String(data.circular_dependencies.length)} detail="Detected import cycles"/></div>
    <p className="workspace-muted">Commit <code>{data.snapshot_sha.slice(0, 12)}</code> · {data.repository}</p>
    {data.circular_dependencies.length > 0 && <details><summary>Potential circular imports</summary><ul>{data.circular_dependencies.map((cycle, index) => <li key={`${index}-${cycle.join('-')}`}>{cycle.join(' → ')}</li>)}</ul></details>}
    <details><summary>Python definitions and imports</summary>{data.python_modules.map((module) => <article key={module.path}><h4><code>{module.path}</code> <small>{module.parse_status}</small></h4><p>{module.definitions.map((item) => `${item.kind} ${item.name}`).join(' · ') || 'No declarations found'}</p><small>Imports: {module.imports.join(', ') || 'none detected'}</small></article>)}</details>
    {data.limitations.map((item) => <p key={item} className="workspace-muted">{item}</p>)}
  </div>;
}

function AnalyticsValue({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <article className="analytics-panel"><small>{label}</small><strong className="analytics-value">{value}</strong><span>{detail}</span></article>;
}

function getIssueData(review: ReviewResponse): { mode: 'canonical'; issues: CanonicalIssue[]; legacyFindings: ReviewFinding[] } | { mode: 'legacy'; issues: []; legacyFindings: ReviewFinding[] } {
  const findings = Array.isArray(review.findings) ? review.findings : [];
  const issues = Array.isArray(review.canonical_issues) ? review.canonical_issues : null;
  const isLegacy = !issues || (issues.length === 0 && review.unique_issue_count == null && findings.length > 0);
  return isLegacy
    ? { mode: 'legacy', issues: [], legacyFindings: findings }
    : { mode: 'canonical', issues: issues ?? [], legacyFindings: findings };
}

function countSources(data: ReturnType<typeof getIssueData>): Record<string, number> {
  if (data.mode === 'canonical') {
    return countBy(data.issues.flatMap((issue) => issue.observations), (item) => item.original_finding.source);
  }
  return countBy(data.legacyFindings, (finding) => finding.source);
}

function countBy<T>(items: T[], key: (item: T) => string): Record<string, number> {
  return items.reduce<Record<string, number>>((counts, item) => {
    const label = key(item);
    counts[label] = (counts[label] ?? 0) + 1;
    return counts;
  }, {});
}

function CanonicalIssueCard({ issue, review, accountId, onFixDecision }: { issue: CanonicalIssue; review: ReviewResponse; accountId: string; onFixDecision: Props['onFixDecision'] }) {
  const observations = Array.isArray(issue.observations) ? issue.observations : [];
  return <details className="workspace-issue">
    <summary>
      <SeverityLabel severity={issue.severity}/>
      <span className="workspace-issue-summary"><strong>{issue.normalized_description}</strong><span><code>{issue.file_path}</code>{formatLine(issue.line_start, issue.line_end)}<em>{pretty(issue.category)}</em></span></span>
      <span className={`workspace-issue-status issue-${issue.status}`}>{pretty(issue.status)}</span>
      <span className="workspace-observation-count">{observations.length} {observations.length === 1 ? 'source' : 'sources'}</span>
    </summary>
    <div className="workspace-issue-body">
      <div className="workspace-issue-context"><span>Issue ID <code>{issue.issue_id}</code></span><span>Status: {pretty(issue.status)}</span></div>
      <IssueExplanationPanel explanation={issue.explanation} status={getIssueExplanationMessage(review, issue.explanation)}/>
      {issue.suggested_fixes?.length ? <section className="workspace-fixes"><h3>Suggested fixes</h3><p>Suggestions never modify GitHub. Acceptance records only your decision in PR Mentor.</p>{issue.suggested_fixes.map((fix) => <SuggestedFixCard key={fix.fix_id} fix={fix} issue={issue} review={review} accountId={accountId} onDecision={onFixDecision}/>)}</section> : null}
      <h3>Evidence from {observations.length} {observations.length === 1 ? 'source' : 'sources'}</h3>
      {observations.length
        ? <div className="workspace-observations">{observations.map((observation, index) => <ObservationCard observation={observation} key={`${observation.original_finding.id}-${index}`}/>)}</div>
        : <p className="workspace-muted">No source observations were linked to this issue.</p>}
    </div>
  </details>;
}

function SuggestedFixCard({ fix, issue, review, accountId, onDecision }: { fix: SuggestedFix; issue: CanonicalIssue; review: ReviewResponse; accountId: string; onDecision: Props['onFixDecision'] }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [semanticBusy, setSemanticBusy] = useState(false);
  const [semanticError, setSemanticError] = useState('');
  const priorSemantic = review.tool_results?.find((item) => item.tool === 'semantic_review' && item.issue_id === issue.issue_id && item.fix_id === fix.fix_id);
  const [semantic, setSemantic] = useState(priorSemantic ?? null);
  const [validationBusy, setValidationBusy] = useState(false);
  const [validationError, setValidationError] = useState('');
  const [validationReport, setValidationReport] = useState<FixValidationReport | null>(fix.validation_reports?.[fix.validation_reports.length - 1] ?? null);
  const [generationBusy, setGenerationBusy] = useState(false);
  const [generationError, setGenerationError] = useState('');
  const [generatedTests, setGeneratedTests] = useState<GeneratedTestCase[]>([]);
  const [executionBusy, setExecutionBusy] = useState(false);
  const [executionError, setExecutionError] = useState('');
  const [executionJob, setExecutionJob] = useState<TestExecutionJob | null>(null);
  const executionStorageKey = `prmentor-execution:${accountId}:${review.review_id}:${issue.issue_id}:${fix.fix_id}`;
  const freshness = review.tool_results?.find((item) => item.tool === 'fix_analysis')?.freshness?.per_proposal ?? [];
  const current = freshness.some((item) => item.fix_id === fix.fix_id && item.canonical_issue_id === issue.issue_id && item.status === 'current');
  const decide = async (decision: 'accepted' | 'rejected') => {
    setBusy(true); setError('');
    try { await onDecision(issue.issue_id, fix.fix_id, decision); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The decision could not be saved.'); }
    finally { setBusy(false); }
  };
  const runSemanticReview = async () => {
    setSemanticBusy(true); setSemanticError('');
    try { setSemantic(await requestSemanticReview(review.review_id, issue.issue_id, fix.fix_id)); }
    catch (reason) { setSemanticError(reason instanceof Error ? reason.message : 'AI assessment is unavailable.'); }
    finally { setSemanticBusy(false); }
  };
  const runValidation = async () => {
    setValidationBusy(true); setValidationError('');
    try {
      const outcome = await validateSuggestedFix(review.review_id, issue.issue_id, fix.fix_id);
      setValidationReport(outcome.report);
    } catch (reason) { setValidationError(reason instanceof Error ? reason.message : 'Validation could not be completed.'); }
    finally { setValidationBusy(false); }
  };
  const generateTests = async () => {
    setGenerationBusy(true); setGenerationError(''); setExecutionError('');
    try {
      const result = await generateTestsForFix(review.review_id, issue.issue_id, fix.fix_id);
      setGeneratedTests(result.tests);
      if (!result.tests.length) setGenerationError(result.reason || `Test generation returned ${result.status}.`);
    } catch (reason) { setGenerationError(reason instanceof Error ? reason.message : 'Test generation is unavailable.'); }
    finally { setGenerationBusy(false); }
  };
  const queueTest = async (test: GeneratedTestCase) => {
    setExecutionBusy(true); setExecutionError('');
    try {
      const key = `stage6:${test.test_id}:${test.source_snapshot_id}`.slice(0, 128);
      const job = await enqueueTestExecution(review.review_id, issue.issue_id, fix.fix_id, test, key);
      setExecutionJob(job);
      sessionStorage.setItem(executionStorageKey, job.execution_id);
    } catch (reason) { setExecutionError(reason instanceof Error ? reason.message : 'The test could not be queued.'); }
    finally { setExecutionBusy(false); }
  };
  useEffect(() => {
    let active = true;
    const stored = sessionStorage.getItem(executionStorageKey);
    if (stored) void getTestExecutionJob(stored).then((job) => { if (active) { setExecutionJob(job); if (job.validation_report) setValidationReport(job.validation_report); } }).catch(() => sessionStorage.removeItem(executionStorageKey));
    return () => { active = false; };
  }, [executionStorageKey]);
  useEffect(() => {
    if (!executionJob || !['queued', 'running'].includes(executionJob.status)) return;
    let active = true;
    const timer = window.setInterval(() => {
      void getTestExecutionJob(executionJob.execution_id).then((job) => { if (active) { setExecutionJob(job); if (job.validation_report) setValidationReport(job.validation_report); } })
        .catch((reason: unknown) => { if (active) setExecutionError(reason instanceof Error ? reason.message : 'Execution status is unavailable.'); });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [executionJob?.execution_id, executionJob?.status]);
  const cancelExecution = async () => {
    if (!executionJob) return;
    try { await cancelTestExecution(executionJob.execution_id); setExecutionJob(await getTestExecutionJob(executionJob.execution_id)); }
    catch (reason) { setExecutionError(reason instanceof Error ? reason.message : 'Cancellation could not be requested.'); }
  };
  return <article className="workspace-fix"><div className="workspace-fix-heading"><strong>{fix.change_summary}</strong><span className={`workspace-fix-status fix-${fix.decision_status}`}>{fix.decision_status}</span></div>
    <p>{fix.rationale}</p><div className="workspace-fix-meta"><code>{fix.file_path}:{fix.start_line}–{fix.end_line}</code><span>{Math.round(fix.confidence * 100)}% model confidence</span><span>{current ? 'Snapshot current at review time' : 'Current snapshot not verified'}</span></div>
    <details><summary>Review proposed code</summary><div className="workspace-code-grid"><div><h4>Original</h4><pre><code>{fix.expected_original_code}</code></pre></div><div><h4>Proposal</h4><pre><code>{fix.proposed_code}</code></pre></div></div></details>
    {!review.is_demo && <button type="button" className="secondary-button" disabled={validationBusy} onClick={() => void runValidation()}>{validationBusy ? 'Checking snapshot and source…' : 'Run source validation'}</button>}
    {!review.is_demo && <button type="button" className="secondary-button" disabled={generationBusy} onClick={() => void generateTests()}>{generationBusy ? 'Checking source and generating…' : 'Generate candidate tests'}</button>}
    {generationError && <p role="status" className="workspace-muted">{generationError}</p>}
    {generatedTests.map((test) => <details key={test.test_id} className="workspace-validation-report"><summary>{test.test_name} · {test.quality_assessment.outcome} · {test.execution_status}</summary><p>{test.test_description}</p><pre><code>{test.test_code}</code></pre><button type="button" className="secondary-button" disabled={executionBusy || !current} onClick={() => void queueTest(test)}>{executionBusy ? 'Queueing…' : 'Run in isolated worker'}</button></details>)}
    {executionJob && <details className="workspace-validation-report" open><summary>Execution {executionJob.status}</summary><p>{executionJob.error_code ? `Worker status: ${executionJob.error_code}` : executionJob.result?.reason || 'Execution result is reported separately from generated-test quality.'}</p>{['queued', 'running'].includes(executionJob.status) && <button type="button" className="secondary-button" onClick={() => void cancelExecution()}>Cancel execution</button>}{executionJob.result && <><p>Proposed execution: {pretty(executionJob.result.status)} · Regression comparison: {pretty(executionJob.result.regression_status)}</p>{executionJob.result.baseline_execution ? <><p>Baseline execution: {pretty(executionJob.result.baseline_execution.status)} · Exit code: {String(executionJob.result.baseline_execution.exit_code ?? 'unavailable')} · Cleanup confirmed: {String(executionJob.result.baseline_execution.cleanup_confirmed)}</p>{executionJob.result.baseline_execution.stdout && <pre><code>{executionJob.result.baseline_execution.stdout}</code></pre>}{executionJob.result.baseline_execution.stderr && <pre><code>{executionJob.result.baseline_execution.stderr}</code></pre>}</> : <p>Baseline result is missing; regression detection is not verified.</p>}<p>Proposed exit code: {String(executionJob.result.exit_code ?? 'unavailable')} · Cleanup confirmed: {String(executionJob.result.cleanup_confirmed)}</p>{executionJob.result.stdout && <pre><code>{executionJob.result.stdout}</code></pre>}{executionJob.result.stderr && <pre><code>{executionJob.result.stderr}</code></pre>}</>}</details>}
    {executionError && <p role="alert" className="workspace-fix-error">{executionError}</p>}
    {validationReport && <details className="workspace-validation-report" open><summary>Attempt {validationReport.attempt_number}: {pretty(validationReport.status)}</summary><p>{validationReport.reason || 'Deterministic checks against a freshly verified snapshot.'}</p>{validationReport.checks.map((check) => <article key={check.check_id}><strong>{check.check_name}</strong><span> · {pretty(check.status)}{check.required ? ' · required' : ' · optional'}</span>{check.reason && <p>{check.reason}</p>}{check.evidence.length > 0 && <small>{check.evidence.length} evidence references</small>}</article>)}{validationReport.report_failure && <p role="status">Report-level issue: {validationReport.report_failure.reason}</p>}<small>Passing checks provide evidence only for the work they performed; they do not prove the fix correct.</small></details>}
    {validationError && <p role="alert" className="workspace-fix-error">{validationError}</p>}
    {fix.validation_reports?.map((report) => <details key={report.validation_id}><summary>Validation attempt {report.attempt_number}: {report.status}</summary><p>{report.reason || 'No report summary was included.'}</p>{report.checks.map((check) => <p key={check.check_id}><strong>{check.check_name}</strong> — {check.status}{check.reason ? `: ${check.reason}` : ''}</p>)}</details>)}
    {!review.is_demo && current && <button type="button" className="secondary-button" disabled={semanticBusy} onClick={() => void runSemanticReview()}>{semanticBusy ? 'Reviewing…' : semantic ? 'Refresh independent AI view' : 'Request independent AI review'}</button>}
    {semantic?.assessment && <div className="workspace-semantic"><strong>AI assessment · {pretty(semantic.assessment.conclusion)} · not deterministic verification</strong><p>{semantic.assessment.rationale}</p><p>Issue coverage: {pretty(semantic.assessment.issue_coverage)} · Test coverage: {pretty(semantic.assessment.generated_test_coverage)}</p>{[...semantic.assessment.logical_risks, ...semantic.assessment.missed_edge_cases, ...semantic.assessment.evidence_conflicts].length > 0 && <ul>{[...semantic.assessment.logical_risks, ...semantic.assessment.missed_edge_cases, ...semantic.assessment.evidence_conflicts].map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>}</div>}
    {semantic?.status === 'unavailable' && <p className="workspace-muted">AI assessment unavailable: {String(semantic.reason || 'No result was returned.')}</p>}{semanticError && <p role="alert" className="workspace-fix-error">{semanticError}</p>}
    {!review.is_demo && <div className="workspace-fix-actions"><button type="button" disabled={busy || fix.decision_status === 'accepted' || !current} title={!current ? 'Acceptance requires a current snapshot result.' : undefined} onClick={() => void decide('accepted')}>{busy ? 'Saving…' : 'Accept'}</button><button type="button" disabled={busy || fix.decision_status === 'rejected'} onClick={() => void decide('rejected')}>Reject</button></div>}
    {!current && <small>Refresh the live review to check snapshot freshness before accepting this proposal.</small>}{error && <p role="alert" className="workspace-fix-error">{error}</p>}
  </article>;
}

function ObservationCard({ observation }: { observation: NonNullable<CanonicalIssue['observations']>[number] }) {
  const finding = observation.original_finding;
  return <article className="workspace-observation">
    <div className="workspace-observation-head"><span className="source-label">{sourceLabel(finding.source)}</span><span>{observation.tool_name || observation.provider_name || 'Source name unavailable'}</span>{observation.rule_id && <code>{observation.rule_id}</code>}</div>
    <p className="workspace-observation-message">{finding.description || finding.title || 'No original message was provided.'}</p>
    <div className="workspace-observation-meta"><code>{finding.file_path || 'File unavailable'}</code><span>{formatLine(finding.line_start, finding.line_end)}</span><span>{formatConfidence(finding.confidence)}</span></div>
    <details className="workspace-evidence"><summary>Original evidence and context</summary>
      {finding.evidence?.length ? <ul>{finding.evidence.map((item, index) => <li key={`${index}-${item}`}><code>{item}</code></li>)}</ul> : <p className="workspace-muted">No evidence was included.</p>}
      {finding.why_it_matters && <p><strong>Why it matters</strong>{finding.why_it_matters}</p>}
      {finding.limitations?.length > 0 && <ul>{finding.limitations.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>}
    </details>
  </article>;
}

function LegacyFindingCard({ finding }: { finding: ReviewFinding }) {
  return <article className="workspace-legacy-finding"><div><SeverityLabel severity={finding.severity}/><span className="source-label">{sourceLabel(finding.source)}</span></div><h3>{finding.title || 'Finding'}</h3><p>{finding.description || 'No description was provided.'}</p><div className="workspace-observation-meta"><code>{finding.file_path || 'File unavailable'}</code><span>{formatLine(finding.line_start, finding.line_end)}</span><span>{formatConfidence(finding.confidence)}</span></div>{hasLegacyExplanation(finding) && <section className="workspace-legacy-guidance" aria-label="Existing guidance on legacy finding"><strong>Guidance on this original finding</strong>{finding.why_it_matters && <p><b>Why it matters</b>{finding.why_it_matters}</p>}{finding.suggested_fix && <p><b>Suggested next step</b>{finding.suggested_fix}</p>}{finding.learning_explanation && <p><b>Learning context</b>{finding.learning_explanation}</p>}</section>}<details className="workspace-evidence"><summary>Original evidence and context</summary>{finding.evidence?.length ? <ul>{finding.evidence.map((item, index) => <li key={`${index}-${item}`}><code>{item}</code></li>)}</ul> : <p className="workspace-muted">No evidence was included.</p>}{finding.why_it_matters && <p><strong>Why it matters</strong>{finding.why_it_matters}</p>}{finding.limitations?.length > 0 && <ul>{finding.limitations.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul>}</details></article>;
}

function IssueExplanationPanel({ explanation, status }: { explanation?: IssueExplanation | null; status: string }) {
  if (!explanation) return <div className="workspace-issue-explanation workspace-explanation-empty" role="status"><Sparkles size={14}/><p>{status}</p></div>;
  const resources = Array.isArray(explanation.learning_resources) ? explanation.learning_resources.filter(isLearningResource) : [];
  const caveats = Array.isArray(explanation.caveats) ? explanation.caveats : [];
  const code = explanation.code_example;
  const hasContent = Boolean(explanation.summary || explanation.why_it_matters || explanation.root_cause
    || explanation.correction || explanation.concept || resources.length || caveats.length
    || (code?.before && code.after));
  if (!hasContent) return <div className="workspace-issue-explanation workspace-explanation-empty" role="status"><Sparkles size={14}/><p>Explanation details were not included for this issue.</p></div>;
  return <section className="workspace-issue-explanation" aria-label="AI-generated educational explanation">
    <div className="workspace-explanation-heading"><Sparkles size={15}/><div><strong>AI-generated guidance</strong><span>Educational interpretation; not a verified fix</span></div></div>
    {explanation.summary && <ExplanationField label="What is the issue?" value={explanation.summary}/>}
    {explanation.why_it_matters && <ExplanationField label="Why it matters" value={explanation.why_it_matters}/>}
    {explanation.root_cause && <ExplanationField label="Root cause" value={explanation.root_cause}/>}
    {explanation.correction && <ExplanationField label="How to address it" value={explanation.correction}/>}
    {code?.before && code.after && <div className="workspace-code-example">
      <div className="workspace-code-heading"><strong>Example approach</strong>{code.language && <span>{code.language}</span>}</div>
      <div className="workspace-code-grid">
        <div><h4>Before</h4><pre><code>{code.before}</code></pre></div>
        <div><h4>After</h4><pre><code>{code.after}</code></pre></div>
      </div>
    </div>}
    {explanation.concept && <ExplanationField label="Concept" value={explanation.concept}/>}
    {resources.length > 0 && <div className="workspace-explanation-field"><h4>Further learning</h4><ul className="workspace-learning-resources">{resources.map((resource, index) => <li key={`${resource.title}-${index}`}><LearningResourceLink resource={resource}/>{resource.description && <p>{resource.description}</p>}</li>)}</ul></div>}
    {caveats.length > 0 && <div className="workspace-explanation-caveats"><strong>Limitations and uncertainty</strong><ul>{caveats.map((caveat, index) => <li key={`${index}-${caveat}`}>{caveat}</li>)}</ul></div>}
  </section>;
}

function ExplanationField({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return <div className="workspace-explanation-field"><h4>{label}</h4><p>{value}</p></div>;
}

const trustedResourceHosts = new Set([
  'bandit.readthedocs.io', 'cheatsheetseries.owasp.org', 'cwe.mitre.org', 'developer.mozilla.org',
  'docs.astral.sh', 'docs.github.com', 'docs.python.org', 'fastapi.tiangolo.com', 'owasp.org', 'peps.python.org',
]);

function LearningResourceLink({ resource }: { resource: LearningResource }) {
  if (!isSafeLearningResource(resource.url)) return <span>{resource.title} <small>(link unavailable)</small></span>;
  return <a href={resource.url} target="_blank" rel="noopener noreferrer">{resource.title}<ExternalLink size={12} aria-hidden="true"/></a>;
}

function isLearningResource(value: LearningResource | null | undefined): value is LearningResource {
  return Boolean(value && typeof value.title === 'string' && value.title.trim() && typeof value.url === 'string');
}

function isSafeLearningResource(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && trustedResourceHosts.has(url.hostname.toLowerCase())
      && !url.username && !url.password && (!url.port || url.port === '443') && !url.search;
  } catch {
    return false;
  }
}

function ExplanationGenerationStatus({ review, legacy }: { review: ReviewResponse; legacy: boolean }) {
  const tool = review.tool_results?.find((item) => item.tool === 'issue_explanations');
  if (legacy) return <p className="workspace-explanation-status" role="status">This legacy review has no canonical explanation status. Existing finding content is preserved.</p>;
  if (!tool) return <p className="workspace-explanation-status" role="status">No explanation-generation status was recorded for this review.</p>;
  const hasCounts = Number.isFinite(tool.explained_issue_count) && Number.isFinite(tool.issue_count);
  const message = tool.status === 'completed'
    ? hasCounts
      ? `${tool.explained_issue_count} of ${tool.issue_count} canonical issues have an explanation.`
      : 'Explanation generation completed. Available explanations are shown on their issues.'
    : tool.status === 'partial'
      ? 'Explanation generation partially completed. Available guidance is shown on its issue; other issues keep their original evidence.'
      : tool.status === 'unavailable'
        ? tool.executed === false
          ? 'Explanation generation was unavailable. Findings and source evidence are still available.'
          : 'Explanation generation failed. Findings and source evidence are still available.'
        : tool.status === 'not_requested'
          ? 'Explanations were not requested for this review.'
          : tool.status === 'not_needed'
            ? 'No canonical issues were returned, so no explanations were needed.'
            : 'Explanation status is not available for this review.';
  return <p className={`workspace-explanation-status status-${tool.status}`} role="status">{message}</p>;
}

function getIssueExplanationMessage(review: ReviewResponse, explanation?: IssueExplanation | null) {
  if (explanation) return '';
  const status = review.tool_results?.find((item) => item.tool === 'issue_explanations')?.status;
  if (status === 'partial') return 'No explanation was saved for this issue; generation completed for only some issues.';
  if (status === 'unavailable') {
    const tool = review.tool_results?.find((item) => item.tool === 'issue_explanations');
    return tool?.executed === false
      ? 'Explanation generation was unavailable. This issue and its original evidence are preserved.'
      : 'Explanation generation failed. This issue and its original evidence are preserved.';
  }
  if (status === 'not_requested') return 'No explanation was requested for this review.';
  if (status === 'not_needed') return 'No explanation was generated for this issue.';
  if (status === 'completed') return 'No explanation was returned for this issue. Its reported finding remains available as evidence.';
  return 'No explanation is stored for this issue.';
}

function hasLegacyExplanation(finding: ReviewFinding) {
  return Boolean(finding.why_it_matters || finding.learning_explanation || finding.suggested_fix);
}

function ToolGroup({ title, tools, empty }: { title: string; tools: ToolResult[]; empty: string }) {
  return <section className="workspace-tool-group"><h3>{title}</h3>{tools.length ? tools.map((tool, index) => <article className="workspace-tool" key={`${tool.tool}-${index}`}><div><strong>{pretty(tool.tool)}</strong><span className={`workspace-tool-status status-${tool.status.replace(/[^a-z]/gi, '-').toLowerCase()}`}>{tool.executed === false ? 'Not executed' : pretty(tool.status)}</span></div><p>{tool.reason || tool.message || (tool.findings ? `${tool.findings.length} diagnostics reported` : tool.executed === false ? 'The backend reports that this stage did not execute.' : 'Status returned by the backend.')}</p></article>) : <p className="workspace-muted">{empty}</p>}</section>;
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <article className="workspace-metric"><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function Distribution({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts).filter(([, count]) => Number.isFinite(count) && count > 0).sort(([left], [right]) => left.localeCompare(right));
  return <article className="workspace-distribution"><h3>{title}</h3>{entries.length ? <div>{entries.map(([label, count]) => <span key={label}><i>{pretty(label)}</i><b>{count}</b></span>)}</div> : <p className="workspace-muted">No counts available.</p>}</article>;
}

function EmptyNotice({ title, detail }: { title: string; detail: string }) {
  return <div className="workspace-empty"><Activity size={17}/><div><strong>{title}</strong><p>{detail}</p></div></div>;
}

function SeverityLabel({ severity }: { severity: ReviewFinding['severity'] }) {
  return <span className={`severity-badge severity-${severity}`}><i/>{pretty(severity)}</span>;
}

function sourceLabel(source: string) {
  return ({ static_analysis: 'Static analysis', ai_candidate: 'AI candidate', test_evidence: 'Test evidence', combined: 'Combined evidence', demo: 'Demo sample' } as Record<string, string>)[source] ?? pretty(source);
}

function formatLine(start: number | null, end: number | null) {
  if (start == null && end == null) return 'Line unavailable';
  if (start == null) return `Line ${end}`;
  if (end == null || end === start) return `Line ${start}`;
  return `Lines ${start}–${end}`;
}

function formatConfidence(confidence: number | null | undefined) {
  return typeof confidence === 'number' && Number.isFinite(confidence) ? `${Math.round(confidence * 100)}% confidence` : 'Confidence unavailable';
}

function formatDate(value?: string) {
  if (!value) return 'Date unavailable';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Date unavailable' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
}

function pretty(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}
