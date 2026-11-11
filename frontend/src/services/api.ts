import type {
  AnalysisMode,
  ApiErrorBody,
  DiffResponse,
  LearningExercise,
  LearningHistory,
  LearningProgress,
  LearningResult,
  ReviewResponse,
  SavedPullRequest,
} from '../types/review';
import { authenticatedFetch, getGitHubProviderToken } from './auth';

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'https://pr-mentor.onrender.com').replace(/\/$/, '');

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await authenticatedFetch(`${API_BASE}${path}`, {
      ...init,
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
    throw new Error(detail || `The PR Mentor API returned ${response.status}.`);
  }
  return response.json() as Promise<T>;
}

export async function analyzePullRequest(url: string, mode: AnalysisMode): Promise<ReviewResponse> {
  const githubToken = mode === 'live' ? await getGitHubProviderToken() : undefined;
  return request<ReviewResponse>('/api/reviews/analyze', {
    method: 'POST',
    headers: githubToken ? { 'X-GitHub-Token': githubToken } : undefined,
    body: JSON.stringify({ url, mode, include_learning: true }),
  });
}

export function getReviewHistory(): Promise<ReviewResponse[]> {
  return request<ReviewResponse[]>('/api/reviews/history');
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
