import type {
  AnalysisMode,
  ApiErrorBody,
  DiffResponse,
  LearningExercise,
  LearningHistory,
  LearningProgress,
  LearningResult,
  ReviewResponse,
  PersonalAnalytics,
  LearningInsights,
  RepositoryIntelligence,
  ToolResult,
  FixValidationReport,
  SavedPullRequest,
  GeneratedTestCase,
  TestExecutionJob,
} from '../types/review';

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export interface CurrentUser { user_id: string; email: string }
export type WorkspaceRole = 'owner' | 'admin' | 'editor' | 'viewer';
export interface WorkspaceMember { user_id: string; email: string; role: WorkspaceRole; joined_at: string }
export interface Workspace { workspace_id: string; name: string; created_by: string; created_at: string; role: WorkspaceRole; members?: WorkspaceMember[] }
export interface WorkspaceInvitation { workspace_id: string; email: string; role: Exclude<WorkspaceRole, 'owner'>; token: string; expires_at: string }
export interface BenchmarkDataset { dataset_id: string; name: string; cases: Array<Record<string, unknown>>; created_at: string; updated_at: string }
export interface BenchmarkRun { run_id: string; created_at: string; status: 'completed' | 'incomplete'; case_count: number; labeled_case_count: number; missing_label_case_count: number; missing_prediction_case_count: number; metrics: Record<string, number | null>; model_configurations: Array<Record<string, unknown> & { provider: string | null; model: string | null }>; case_results: Array<Record<string, unknown>>; limitations: string[] }

export class ApiRequestError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = 'ApiRequestError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: 'include',
      headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
    });
  } catch {
    throw new Error('The PR Mentor API could not be reached. Check that the backend is running and try again.');
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as ApiErrorBody;
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => item.msg).filter(Boolean).join(' ')
      : body.detail ?? body.error?.message;
    throw new ApiRequestError(detail || `The PR Mentor API returned ${response.status}.`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function getCurrentUser(): Promise<CurrentUser> { return request<CurrentUser>('/api/auth/me'); }
export function register(email: string, password: string): Promise<CurrentUser> {
  return request<CurrentUser>('/api/auth/register', { method: 'POST', body: JSON.stringify({ email, password }) });
}
export function login(email: string, password: string): Promise<CurrentUser> {
  return request<CurrentUser>('/api/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) });
}
export function logout(): Promise<void> { return request<void>('/api/auth/logout', { method: 'POST' }); }

export function listWorkspaces(): Promise<Workspace[]> { return request<Workspace[]>('/api/workspaces'); }
export function getWorkspace(workspaceId: string): Promise<Workspace> { return request<Workspace>(`/api/workspaces/${encodeURIComponent(workspaceId)}`); }
export function createWorkspace(name: string): Promise<Workspace> { return request<Workspace>('/api/workspaces', { method: 'POST', body: JSON.stringify({ name }) }); }
export function inviteWorkspaceMember(workspaceId: string, email: string, role: Exclude<WorkspaceRole, 'owner'>): Promise<WorkspaceInvitation> {
  return request<WorkspaceInvitation>(`/api/workspaces/${encodeURIComponent(workspaceId)}/invitations`, { method: 'POST', body: JSON.stringify({ email, role }) });
}
export function acceptWorkspaceInvitation(token: string): Promise<{ workspace_id: string; role: WorkspaceRole }> {
  return request<{ workspace_id: string; role: WorkspaceRole }>('/api/workspaces/invitations/accept', { method: 'POST', body: JSON.stringify({ token }) });
}
export function updateWorkspaceMemberRole(workspaceId: string, userId: string, role: Exclude<WorkspaceRole, 'owner'>): Promise<{ workspace_id: string; user_id: string; role: WorkspaceRole }> {
  return request<{ workspace_id: string; user_id: string; role: WorkspaceRole }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`, { method: 'PATCH', body: JSON.stringify({ role }) });
}
export function removeWorkspaceMember(workspaceId: string, userId: string): Promise<{ removed: boolean }> {
  return request<{ removed: boolean }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`, { method: 'DELETE' });
}
export function shareWorkspaceReview(workspaceId: string, reviewId: string): Promise<{ shared: boolean }> {
  return request<{ shared: boolean }>(`/api/workspaces/${encodeURIComponent(workspaceId)}/reviews/${encodeURIComponent(reviewId)}`, { method: 'POST' });
}
export function listWorkspaceReviews(workspaceId: string): Promise<ReviewResponse[]> {
  return request<ReviewResponse[]>(`/api/workspaces/${encodeURIComponent(workspaceId)}/reviews`);
}
export function getWorkspaceLearningInsights(workspaceId: string): Promise<LearningInsights> {
  return request<LearningInsights>(`/api/workspaces/${encodeURIComponent(workspaceId)}/learning-insights`);
}

export function listBenchmarkDatasets(): Promise<BenchmarkDataset[]> { return request<BenchmarkDataset[]>('/api/benchmarks'); }
export function createBenchmarkDataset(name: string, cases: Array<Record<string, unknown>>): Promise<BenchmarkDataset> {
  return request<BenchmarkDataset>('/api/benchmarks', { method: 'POST', body: JSON.stringify({ name, cases }) });
}
export function updateBenchmarkLabels(datasetId: string, cases: Array<Record<string, unknown>>): Promise<BenchmarkDataset> {
  return request<BenchmarkDataset>(`/api/benchmarks/${encodeURIComponent(datasetId)}/labels`, { method: 'PUT', body: JSON.stringify({ cases }) });
}
export function runBenchmark(datasetId: string): Promise<BenchmarkRun> {
  return request<BenchmarkRun>(`/api/benchmarks/${encodeURIComponent(datasetId)}/runs`, { method: 'POST' });
}
export function listBenchmarkRuns(datasetId: string): Promise<BenchmarkRun[]> {
  return request<BenchmarkRun[]>(`/api/benchmarks/${encodeURIComponent(datasetId)}/runs`);
}

export function analyzePullRequest(url: string, mode: AnalysisMode): Promise<ReviewResponse> {
  return request<ReviewResponse>('/api/reviews/analyze', {
    method: 'POST',
    body: JSON.stringify({ url, mode, include_learning: true }),
  });
}

export interface ReviewJob { job_id: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'; review_id?: string | null; error_code?: string | null }
export function enqueueReview(url: string, mode: AnalysisMode): Promise<ReviewJob> {
  return request<ReviewJob>('/api/reviews/jobs', { method: 'POST', body: JSON.stringify({ url, mode, include_learning: true }) });
}
export function getReviewJob(jobId: string): Promise<ReviewJob> {
  return request<ReviewJob>(`/api/reviews/jobs/${encodeURIComponent(jobId)}`);
}

export function getReviewHistory(limit = 50, includeDemo = true): Promise<ReviewResponse[]> {
  return request<ReviewResponse[]>(`/api/reviews/history?limit=${encodeURIComponent(String(limit))}&include_demo=${includeDemo}`);
}

export function getReview(reviewId: string): Promise<ReviewResponse> {
  return request<ReviewResponse>(`/api/reviews/${encodeURIComponent(reviewId)}`);
}

export function getReviewDiff(reviewId: string): Promise<DiffResponse> {
  return request<DiffResponse>(`/api/reviews/${encodeURIComponent(reviewId)}/diff`);
}

export function getSavedPullRequests(): Promise<SavedPullRequest[]> {
  return request<SavedPullRequest[]>('/api/reviews/saved');
}

export function savePullRequest(url: string): Promise<SavedPullRequest> {
  return request<SavedPullRequest>('/api/reviews/saved', {
    method: 'POST',
    body: JSON.stringify({ url }),
  });
}

export function deleteSavedPullRequest(savedId: string): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(`/api/reviews/saved/${encodeURIComponent(savedId)}`, { method: 'DELETE' });
}

export function setSuggestedFixDecision(reviewId: string, issueId: string, fixId: string, decision: 'accepted' | 'rejected') {
  return request(`/api/reviews/${encodeURIComponent(reviewId)}/issues/${encodeURIComponent(issueId)}/fixes/${encodeURIComponent(fixId)}/decision`, {
    method: 'POST', body: JSON.stringify({ decision }),
  });
}

export function getLearningExercises(): Promise<LearningExercise[]> {
  return request<LearningExercise[]>('/api/learning/exercises');
}

export function submitExercise(exerciseId: string, answer: string): Promise<LearningResult> {
  return request<LearningResult>(`/api/learning/exercises/${encodeURIComponent(exerciseId)}/submit`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  });
}

export function getLearningProgress(): Promise<LearningProgress> {
  return request<LearningProgress>('/api/learning/progress');
}

export function getLearningHistory(): Promise<LearningHistory> {
  return request<LearningHistory>('/api/learning/history');
}

export function getPersonalAnalytics(): Promise<PersonalAnalytics> { return request<PersonalAnalytics>('/api/analytics/me'); }
export function getLearningInsights(): Promise<LearningInsights> { return request<LearningInsights>('/api/learning/insights'); }

export function getRepositoryIntelligence(owner: string, repository: string, ref: string): Promise<RepositoryIntelligence> {
  const query = new URLSearchParams({ ref });
  return request<RepositoryIntelligence>(`/api/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/intelligence?${query}`);
}

export function requestSemanticReview(reviewId: string, issueId: string, fixId: string): Promise<ToolResult> {
  return request<ToolResult>(`/api/reviews/${encodeURIComponent(reviewId)}/issues/${encodeURIComponent(issueId)}/fixes/${encodeURIComponent(fixId)}/semantic-review`, { method: 'POST' });
}

export function validateSuggestedFix(reviewId: string, issueId: string, fixId: string): Promise<{
  report: FixValidationReport;
  tool_results: Record<string, unknown>;
  generation: null;
  execution: 'not_run';
}> {
  return request(`/api/reviews/${encodeURIComponent(reviewId)}/issues/${encodeURIComponent(issueId)}/fixes/${encodeURIComponent(fixId)}/validate`, { method: 'POST' });
}

export function generateTestsForFix(reviewId: string, issueId: string, fixId: string): Promise<{ status: string; tests: GeneratedTestCase[]; reason?: string | null; limitations: string[] }> {
  return request(`/api/reviews/${encodeURIComponent(reviewId)}/issues/${encodeURIComponent(issueId)}/fixes/${encodeURIComponent(fixId)}/generate-tests`, { method: 'POST' });
}

export function enqueueTestExecution(reviewId: string, issueId: string, fixId: string, test: GeneratedTestCase, idempotencyKey: string): Promise<TestExecutionJob> {
  return request(`/api/reviews/${encodeURIComponent(reviewId)}/issues/${encodeURIComponent(issueId)}/fixes/${encodeURIComponent(fixId)}/tests/execute`, {
    method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(test),
  });
}

export function getTestExecutionJob(executionId: string): Promise<TestExecutionJob> {
  return request(`/api/reviews/execution-jobs/${encodeURIComponent(executionId)}`);
}

export function cancelTestExecution(executionId: string): Promise<{ status: string }> {
  return request(`/api/reviews/execution-jobs/${encodeURIComponent(executionId)}`, { method: 'DELETE' });
}
