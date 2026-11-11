export type AnalysisMode = 'demo' | 'live';
export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'informational';
export type VerificationStatus = 'verified' | 'needs_review' | 'unsupported';
export type FindingSource = 'static_analysis' | 'ai_candidate' | 'test_evidence' | 'combined' | 'demo';

export interface PullRequestMetadata {
  repository_owner: string;
  repository_name: string;
  repository_url: string;
  pull_request_number: number;
  pull_request_url: string;
  title: string;
  description: string | null;
  author: string | null;
  base_branch: string | null;
  head_branch: string | null;
  head_sha: string | null;
  state: string;
  changed_files: number;
  additions: number;
  deletions: number;
  repository?: string;
  pull_request_title?: string;
  url?: string;
}

export interface ReviewFinding {
  id: string;
  title: string;
  description: string;
  file_path: string;
  line_start: number | null;
  line_end: number | null;
  severity: Severity;
  category: 'bug' | 'security' | 'performance' | 'maintainability' | 'code_quality' | 'testing' | 'documentation' | 'learning';
  confidence: number;
  source: FindingSource;
  verification_status: VerificationStatus;
  evidence: string[];
  why_it_matters: string;
  suggested_fix: string;
  learning_explanation: string;
  limitations: string[];
  file?: string;
  line?: number | string;
  text?: string;
  proof?: string;
  status?: string;
  severity_label?: string;
}

export interface ReviewResponse {
  review_id: string;
  workspace_id?: string | null;
  status: string;
  analysis_mode: AnalysisMode;
  is_demo: boolean;
  model_provider?: string | null;
  model_name?: string | null;
  model_usage?: Record<string, number> | null;
  model_fallback_used?: boolean | null;
  model_latency_ms?: number | null;
  model_configuration?: Record<string, string | number | boolean> | null;
  pull_request: PullRequestMetadata;
  summary: string;
  findings: ReviewFinding[];
  canonical_issues?: CanonicalIssue[];
  unique_issue_count?: number | null;
  raw_observation_count?: number | null;
  observation_counts_by_source?: Record<string, number>;
  issue_counts_by_severity?: Record<string, number>;
  issue_counts_by_category?: Record<string, number>;
  verification_summary: Record<string, number>;
  tool_results: ToolResult[];
  warnings: string[];
  created_at: string;
  duration_ms: number;
  diff_files?: DiffFile[];
  repository?: string;
  pull_request_title?: string;
  pull_request_number?: number;
  url?: string;
}

export interface FindingObservation {
  original_finding: ReviewFinding;
  tool_name?: string | null;
  provider_name?: string | null;
  rule_id?: string | null;
}

export interface CanonicalIssue {
  issue_id: string;
  file_path: string;
  line_start: number | null;
  line_end: number | null;
  category: ReviewFinding['category'];
  severity: Severity;
  normalized_description: string;
  status: 'open' | 'resolved' | 'dismissed';
  observations: FindingObservation[];
  explanation?: IssueExplanation | null;
  suggested_fixes?: SuggestedFix[];
}

export type FixGenerationStatus = 'generated' | 'stale' | 'conflicted';
export type FixDecisionStatus = 'pending' | 'accepted' | 'rejected';
export type FixValidationStatus = 'passed' | 'failed' | 'partial' | 'inconclusive' | 'unavailable' | 'not_run';
export type FixValidationCheckStatus = FixValidationStatus | 'skipped';
export type FixValidationProvenanceProfile = 'source_analysis' | 'snapshot_integrity' | 'ai_assessment' | 'test_execution' | 'non_source' | 'legacy_unverified';

export interface FixValidationReportFailure {
  code: string;
  status: 'failed' | 'unavailable' | 'inconclusive';
  reason: string;
}

export interface FixValidationEvidenceReference {
  evidence_id: string;
  kind: 'source_snapshot' | 'tool_result' | 'finding' | 'test_result' | 'syntax_error' | 'diff' | 'execution_log' | 'other';
  description: string;
  file_path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  sha256?: string | null;
}

export interface FixValidationSnapshot {
  file_path: string;
  base_commit_sha?: string | null;
  checked_head_sha?: string | null;
  baseline_blob_sha?: string | null;
  proposed_blob_sha?: string | null;
}

export interface FixValidationFindingCounts {
  baseline_count: number;
  proposed_count: number;
  introduced_count: number;
  resolved_count: number;
  unchanged_count: number;
}

export interface FixValidationCheckResult {
  check_id: string;
  check_name: string;
  provenance_profile?: FixValidationProvenanceProfile | null;
  effective_provenance_profile: FixValidationProvenanceProfile;
  status: FixValidationCheckStatus;
  required: boolean;
  tool_name?: string | null;
  tool_version?: string | null;
  configuration_sha256?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  finding_counts?: FixValidationFindingCounts | null;
  evidence: FixValidationEvidenceReference[];
  reason?: string | null;
  limitations: string[];
}

export interface FixValidationReport {
  schema_version: '1' | '2';
  validation_id: string;
  fix_id: string;
  attempt_number: number;
  trigger: 'manual' | 'automatic' | 'retry';
  status: FixValidationStatus;
  engine_version?: string | null;
  started_at: string;
  completed_at?: string | null;
  snapshot?: FixValidationSnapshot | null;
  checks: FixValidationCheckResult[];
  report_failure?: FixValidationReportFailure | null;
  limitations: string[];
  reason?: string | null;
}

export interface SuggestedFix {
  fix_id: string;
  canonical_issue_id: string;
  file_path: string;
  start_line: number;
  end_line: number;
  language: 'python';
  base_commit_sha: string;
  file_blob_sha?: string | null;
  expected_original_code: string;
  proposed_code: string;
  change_summary: string;
  rationale: string;
  limitations: string[];
  confidence: number;
  generation_status: FixGenerationStatus;
  decision_status: FixDecisionStatus;
  generated_at: string;
  decided_at?: string | null;
  validation_reports?: FixValidationReport[];
}

export interface GeneratedTestCase {
  test_id: string;
  suggested_fix_id: string;
  canonical_issue_id: string;
  repository_id: string;
  source_snapshot_id: string;
  source_file_path: string;
  test_name: string;
  test_code: string;
  test_description: string;
  execution_status: 'not_run';
  quality_assessment: { outcome: string; findings: string[] };
  [key: string]: unknown;
}

export type TestExecutionStatus = 'passed' | 'failed' | 'timed_out' | 'cancelled' | 'unavailable' | 'rejected' | 'infrastructure_error' | 'not_run';

export interface TestExecutionJob {
  execution_id: string;
  status: 'queued' | 'running' | Exclude<TestExecutionStatus, 'not_run'>;
  result?: TestExecutionResult | null;
  generated_test?: GeneratedTestCase | null;
  error_code?: string | null;
  validation_report?: FixValidationReport | null;
}

export type RegressionDetectionStatus = 'detected' | 'not_fixed' | 'not_reproduced' | 'regression_introduced' | 'unavailable' | 'inconclusive';

export interface SourceExecutionAttempt {
  variant: 'baseline';
  review_id?: string | null;
  repository_id: string;
  canonical_issue_id: string;
  suggested_fix_id: string;
  source_snapshot_id: string;
  base_commit_sha: string;
  checked_head_sha: string;
  baseline_blob_sha: string;
  proposed_blob_sha: string;
  test_id: string;
  status: TestExecutionStatus;
  runtime_name: string;
  runtime_version?: string | null;
  execution_image_digest?: string | null;
  python_version?: string | null;
  pytest_version?: string | null;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  exit_code?: number | null;
  stdout: string;
  stderr: string;
  output_truncated: boolean;
  timeout_enforced: boolean;
  cleanup_confirmed: boolean;
  reason?: string | null;
}

export interface TestExecutionResult {
  execution_id: string;
  review_id?: string | null;
  repository_id: string;
  canonical_issue_id: string;
  suggested_fix_id: string;
  source_snapshot_id: string;
  base_commit_sha: string;
  checked_head_sha: string;
  baseline_blob_sha: string;
  proposed_blob_sha: string;
  test_id: string;
  status: TestExecutionStatus;
  regression_status: RegressionDetectionStatus;
  baseline_execution?: SourceExecutionAttempt | null;
  exit_code?: number | null;
  cleanup_confirmed: boolean;
  reason?: string | null;
  stdout: string;
  stderr: string;
}

export interface IssueCodeExample {
  before?: string;
  after?: string;
  language?: string | null;
}

export interface LearningResource {
  title: string;
  url: string;
  description?: string | null;
}

export interface IssueExplanation {
  summary?: string;
  why_it_matters?: string;
  root_cause?: string;
  correction?: string;
  code_example?: IssueCodeExample | null;
  concept?: string;
  learning_resources?: LearningResource[];
  caveats?: string[];
}

export interface ToolResult {
  tool: string;
  status: string;
  reason?: string;
  message?: string;
  executed?: boolean;
  exit_code?: number;
  files_received?: number;
  analyzable_patch_files?: number;
  findings?: Array<Record<string, unknown>>;
  issue_count?: number;
  explained_issue_count?: number;
  batch_count?: number;
  failed_batch_count?: number;
  rejected_explanation_count?: number;
  context_failure_count?: number;
  freshness?: {
    per_proposal?: Array<{ canonical_issue_id: string; fix_id: string; status: string; reason?: string }>;
  };
  issue_id?: string;
  fix_id?: string;
  assessment?: {
    issue_coverage: string;
    logical_risks: string[];
    behavior_changes: string[];
    missed_edge_cases: string[];
    generated_test_coverage: string;
    evidence_conflicts: string[];
    conclusion: string;
    rationale: string;
    limitations: string[];
  } | null;
  latency_ms?: number;
}

export interface DiffLine {
  kind: 'added' | 'removed' | 'context';
  line: number;
  text: string;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  previous_path?: string | null;
  status: string;
  added: number;
  removed: number;
  hunks: DiffHunk[];
  truncated: boolean;
}

export interface DiffResponse {
  review_id: string;
  files: DiffFile[];
  warnings: string[];
}

export interface SavedPullRequest {
  saved_id: string;
  url: string;
  temporary: boolean;
}

export interface LearningExercise {
  id: string;
  title: string;
  description: string;
  language: string;
  difficulty: string;
  category: string;
  instructions: string;
  code_snippet: string;
  hints: string[];
  options: string[];
  learning_objectives: string[];
}

export interface LearningResult {
  correct: boolean;
  explanation: string;
  feedback: string;
  points: number;
  progress: LearningProgress;
}

export interface LearningProgress {
  temporary: boolean;
  exercises_completed: number;
  correct_answers: number;
  attempt_count: number;
  accuracy: number;
  topics_practiced: Record<string, number>;
}

export interface LearningHistory {
  temporary: boolean;
  attempts: Array<{ exercise_id: string; category: string; correct: boolean }>;
}

export interface PersonalAnalytics {
  total_reviews: number;
  live_pull_request_count: number;
  live_finding_count: number;
  reviews_analyzed: number;
  analysis_window_limit: number;
  findings_by_category: Record<string, number>;
  findings_by_severity: Record<string, number>;
  suggested_fix_count: number;
  fix_decisions: Record<string, number>;
  validation_reports_by_status: Record<string, number>;
  average_review_duration_ms: number | null;
  model_latency_ms: number | null;
  model_cost: number | null;
  accuracy_metrics: Record<string, number> | null;
  unavailable_metrics: string[];
}

export interface LearningInsights {
  scope: 'personal' | 'workspace';
  source_review_count: number;
  window_limit: number;
  repeated_patterns: Array<{ category: string; observation_count: number }>;
  suggested_topics: string[];
  suggested_fix_count: number;
  fix_decisions: Record<string, number>;
  validation_reports_by_status: Record<string, number>;
  basis: string;
}

export interface RepositoryIntelligence {
  repository: string;
  snapshot_sha: string;
  manifest_count: number;
  manifest_truncated: boolean;
  python_files_considered: number;
  python_modules: Array<{ path: string; parse_status: string; definitions: Array<{ name: string; kind: string; line: number; scope?: string | null }>; imports: string[] }>;
  internal_import_edges: Array<{ from: string; to: string }>;
  circular_dependencies: string[][];
  limitations: string[];
}

export interface ApiErrorBody {
  detail?: string | Array<{ msg?: string }>;
  error?: { message?: string };
}
