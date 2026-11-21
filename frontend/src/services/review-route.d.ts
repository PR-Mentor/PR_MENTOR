import type { ReviewResponse } from '../types/review';

 codex/phone-otp-login
export type ReviewRoutePage = 'discover' | 'repositories' | 'review' | 'history' | 'saved' | 'diff' | 'learning' | 'analytics' | 'profile' | 'workspaces' | 'benchmarks';

export type ReviewRoutePage = 'discover' | 'review' | 'history' | 'saved' | 'diff' | 'learning' | 'analytics' | 'profile' | 'workspaces';
 main
export type ReviewRouteState =
  | { status: 'loading' }
  | { status: 'success'; review: ReviewResponse }
  | { status: 'not_found'; message: string }
  | { status: 'error'; message: string };

export function resolvePageRoute(pathname: string): { page: ReviewRoutePage; reviewId: string | null };
export function loadReviewRoute(
  reviewId: string | null,
  fetchReview: (reviewId: string) => Promise<ReviewResponse>,
  onState: (state: ReviewRouteState) => void,
): Promise<void>;
export function getReviewRouteView(
  reviewId: string | null,
  review: ReviewResponse | null,
  busy: boolean,
  error: string,
  notFound: boolean,
): { review: ReviewResponse | null; busy: boolean; error: string; notFound: boolean };
