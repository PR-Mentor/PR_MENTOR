import assert from 'node:assert/strict';
import test from 'node:test';

import { getReviewRouteView, loadReviewRoute, resolvePageRoute } from './review-route.js';

test('resolves the dedicated review route and extracts decoded IDs', () => {
  assert.deepEqual(resolvePageRoute('/reviews/review-123'), { page: 'review', reviewId: 'review-123' });
  assert.deepEqual(resolvePageRoute('/reviews/demo%2Dabc/'), { page: 'review', reviewId: 'demo-abc' });
});

test('preserves existing application routes', () => {
  assert.deepEqual(resolvePageRoute('/history'), { page: 'history', reviewId: null });
  assert.deepEqual(resolvePageRoute('/diff'), { page: 'diff', reviewId: null });
  assert.deepEqual(resolvePageRoute('/analytics'), { page: 'analytics', reviewId: null });
  assert.deepEqual(resolvePageRoute('/profile'), { page: 'profile', reviewId: null });
  assert.deepEqual(resolvePageRoute('/workspaces'), { page: 'workspaces', reviewId: null });
  assert.deepEqual(resolvePageRoute('/benchmarks'), { page: 'benchmarks', reviewId: null });
  assert.deepEqual(resolvePageRoute('/unknown'), { page: 'discover', reviewId: null });
});

test('direct route loading emits loading then success using the URL ID', async () => {
  const states = [];
  const review = { review_id: 'review-456', findings: [], pull_request: {} };
  const requestedIds = [];

  await loadReviewRoute('review-456', async (id) => {
    requestedIds.push(id);
    return review;
  }, (state) => states.push(state));

  assert.deepEqual(requestedIds, ['review-456']);
  assert.deepEqual(states.map((state) => state.status), ['loading', 'success']);
  assert.equal(states[1].review, review);
});

test('invalid URL IDs and backend 404s become not-found states', async () => {
  const invalidStates = [];
  await loadReviewRoute('', async () => { throw new Error('should not fetch'); }, (state) => invalidStates.push(state));
  assert.deepEqual(invalidStates.map((state) => state.status), ['not_found']);

  const missingStates = [];
  const notFound = Object.assign(new Error('Review not found.'), { status: 404 });
  await loadReviewRoute('missing-id', async () => { throw notFound; }, (state) => missingStates.push(state));
  assert.deepEqual(missingStates.map((state) => state.status), ['loading', 'not_found']);
});

test('backend and network errors become an error state', async () => {
  const states = [];
  await loadReviewRoute('review-789', async () => { throw new Error('API unavailable'); }, (state) => states.push(state));
  assert.deepEqual(states.map((state) => state.status), ['loading', 'error']);
  assert.equal(states[1].message, 'API unavailable');
});

test('legacy review payloads without canonical issues load unchanged', async () => {
  const legacyReview = { review_id: 'legacy-1', findings: [{ id: 'f1' }] };
  const states = [];
  await loadReviewRoute('legacy-1', async () => legacyReview, (state) => states.push(state));
  assert.equal(states[1].review, legacyReview);
  assert.equal('canonical_issues' in states[1].review, false);
});

test('route view keeps stale review data hidden while a new URL ID loads', () => {
  const staleReview = { review_id: 'old-review' };
  assert.deepEqual(getReviewRouteView('new-review', staleReview, false, '', false), {
    review: null, busy: true, error: '', notFound: false,
  });
  assert.deepEqual(getReviewRouteView('missing', staleReview, false, 'Review not found.', true), {
    review: null, busy: false, error: 'Review not found.', notFound: true,
  });
});
