export function resolvePageRoute(pathname) {
  const reviewPath = pathname.match(/^\/reviews(?:\/(.*?))?\/?$/);
  if (reviewPath) {
    let reviewId = reviewPath[1] ?? '';
    try {
      reviewId = decodeURIComponent(reviewId);
    } catch {
      reviewId = '';
    }
    return { page: 'review', reviewId };
  }

  const value = pathname.replace(/^\//, '').replace(/\/$/, '');
  const page = ['review', 'history', 'saved', 'diff', 'learning', 'analytics', 'profile', 'workspaces', 'benchmarks'].includes(value) ? value : 'discover';
  return { page, reviewId: null };
}

export async function loadReviewRoute(reviewId, fetchReview, onState) {
  const id = typeof reviewId === 'string' ? reviewId.trim() : '';
  if (!id) {
    onState({ status: 'not_found', message: 'The review URL does not contain a valid review ID.' });
    return;
  }

  onState({ status: 'loading' });
  try {
    const review = await fetchReview(id);
    onState({ status: 'success', review });
  } catch (error) {
    if (error && error.status === 404) {
      onState({ status: 'not_found', message: error.message || 'This review was not found.' });
      return;
    }
    onState({
      status: 'error',
      message: error instanceof Error ? error.message : 'This review could not be loaded.',
    });
  }
}

export function getReviewRouteView(reviewId, review, busy, error, notFound) {
  const matchesRoute = reviewId === null || review?.review_id === reviewId;
  return {
    review: matchesRoute ? review : null,
    busy: busy || (reviewId !== null && !matchesRoute && !error),
    error,
    notFound,
  };
}
