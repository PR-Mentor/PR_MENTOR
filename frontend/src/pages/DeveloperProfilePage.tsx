import { useEffect, useMemo, useState } from 'react';
import { ArrowUpRight, BookOpen, RefreshCw, UserRound } from 'lucide-react';
import { getLearningHistory, getLearningInsights, getLearningProgress, getPersonalAnalytics, getReviewHistory } from '../services/api';
import type { CurrentUser } from '../services/api';
import type { LearningHistory, LearningInsights, LearningProgress, PersonalAnalytics, ReviewResponse, SuggestedFix } from '../types/review';

interface Props { user: CurrentUser; onOpenReview: (reviewId: string) => void; onOpenLearning: () => void; onOpenHistory: () => void }
type ProfileData = { analytics: PersonalAnalytics; insights: LearningInsights; progress: LearningProgress; learningHistory: LearningHistory; reviews: ReviewResponse[] };
type ProfileIssue = { issue_id: string; category: string; status: string | null; suggested_fixes?: SuggestedFix[] };

function issuesOf(review: ReviewResponse): ProfileIssue[] {
  if (Array.isArray(review.canonical_issues) && review.canonical_issues.length) return review.canonical_issues.map((issue) => ({ issue_id: issue.issue_id, category: issue.category, status: issue.status, suggested_fixes: issue.suggested_fixes }));
  return (review.findings ?? []).map((finding, index) => ({
    issue_id: `${review.review_id}-legacy-${index}`, category: finding.category, status: null,
  }));
}

function label(value: string) { return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatMonth(value: string) { return new Intl.DateTimeFormat(undefined, { month: 'short' }).format(new Date(`${value}-01T00:00:00Z`)); }

export default function DeveloperProfilePage({ user, onOpenReview, onOpenLearning, onOpenHistory }: Props) {
  const [data, setData] = useState<ProfileData | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');

  const load = async () => {
    setBusy(true); setError('');
    try {
      const [analytics, insights, progress, learningHistory, reviews] = await Promise.all([
        getPersonalAnalytics(), getLearningInsights(), getLearningProgress(), getLearningHistory(), getReviewHistory(100, false),
      ]);
      setData({ analytics, insights, progress, learningHistory, reviews });
    } catch (reason) {
      setData(null);
      setError(reason instanceof Error ? reason.message : 'Developer profile data is unavailable.');
    } finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);

  const derived = useMemo(() => {
    if (!data) return null;
    const liveReviews = data.reviews.filter((review) => !review.is_demo && review.analysis_mode !== 'demo');
    const issues = liveReviews.flatMap(issuesOf);
    const lifecycle = issues.reduce((counts, issue) => {
      if (issue.status === 'resolved') counts.resolved += 1;
      else if (issue.status === 'open') counts.open += 1;
      else if (issue.status === 'dismissed') counts.dismissed += 1;
      else counts.unclassified += 1;
      return counts;
    }, { resolved: 0, open: 0, dismissed: 0, unclassified: 0 });
    const monthly = new Map<string, number>();
    for (const review of liveReviews) {
      const date = new Date(review.created_at);
      if (Number.isNaN(date.valueOf())) continue;
      const key = `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
      monthly.set(key, (monthly.get(key) ?? 0) + issuesOf(review).length);
    }
    const timeline = [...monthly.entries()].sort(([a], [b]) => a.localeCompare(b)).slice(-6);
    const categoryRates = new Map<string, { recent: number; earlier: number }>();
    const recent = liveReviews.slice(0, Math.ceil(liveReviews.length / 2));
    const earlier = liveReviews.slice(Math.ceil(liveReviews.length / 2));
    const rate = (reviews: ReviewResponse[], category: string) => reviews.reduce((sum, review) => sum + issuesOf(review).filter((issue) => issue.category === category).length, 0) / Math.max(1, reviews.length);
    const categoryCounts = new Map<string, number>();
    for (const issue of issues) categoryCounts.set(issue.category, (categoryCounts.get(issue.category) ?? 0) + 1);
    const categories = new Set(issues.map((issue) => issue.category));
    for (const category of categories) categoryRates.set(category, { recent: rate(recent, category), earlier: rate(earlier, category) });
    const fixDecisions = new Map<string, number>();
    const validationStatuses = new Map<string, number>();
    let fixCount = 0;
    for (const issue of issues) for (const fix of issue.suggested_fixes ?? []) {
      fixCount += 1;
      if (fix.decision_status === 'accepted' || fix.decision_status === 'rejected') fixDecisions.set(fix.decision_status, (fixDecisions.get(fix.decision_status) ?? 0) + 1);
      for (const report of fix.validation_reports ?? []) validationStatuses.set(report.status, (validationStatuses.get(report.status) ?? 0) + 1);
    }
    const learningRates = new Map<string, { correct: number; attempts: number }>();
    for (const attempt of data.learningHistory.attempts) {
      const current = learningRates.get(attempt.category) ?? { correct: 0, attempts: 0 };
      current.attempts += 1; current.correct += Number(attempt.correct);
      learningRates.set(attempt.category, current);
    }
    return { issues, lifecycle, timeline, categoryRates, categoryCounts, fixDecisions, validationStatuses, fixCount, learningRates, liveReviewCount: liveReviews.length };
  }, [data]);

  if (busy) return <section className="content-page"><div className="page-heading"><div><span className="eyebrow">PERSONAL DEVELOPMENT</span><h1>Developer profile</h1><p>Private summaries from your saved review and learning history.</p></div></div><p role="status">Loading your profile evidence…</p></section>;
  if (!data || !derived) return <section className="content-page"><div className="page-heading"><div><span className="eyebrow">PERSONAL DEVELOPMENT</span><h1>Developer profile</h1></div><button type="button" className="secondary-button" onClick={() => void load()}><RefreshCw size={15}/> Retry</button></div><p className="alert error-alert" role="alert">{error || 'Profile data could not be loaded.'}</p></section>;

  const categoryRows = [...derived.categoryCounts.entries()].sort((a, b) => b[1] - a[1]);
  const maxCategory = Math.max(1, ...categoryRows.map(([, count]) => count));
  const maxMonth = Math.max(1, ...derived.timeline.map(([, count]) => count));
  const sampleIsPartial = data.analytics.total_reviews > data.reviews.length;
  return <section className="content-page developer-profile">
    <div className="page-heading"><div><span className="eyebrow">PERSONAL DEVELOPMENT</span><h1>Developer profile</h1><p>Private activity and learning evidence for this account. Workspace activity is excluded.</p></div><button type="button" className="secondary-button" onClick={() => void load()}><RefreshCw size={15}/> Refresh</button></div>
    <section className="profile-identity"><span className="profile-avatar"><UserRound size={22}/></span><div><strong>{user.email}</strong><small>Personal profile · account {user.user_id.slice(0, 8)}</small></div></section>
    <div className="analytics-cards">
      <Metric label="LIVE REVIEW RECORDS" value={data.analytics.total_reviews.toLocaleString()} detail="Exact account-owned live review count; demos excluded"/>
      <Metric label="DISTINCT LIVE PRS REVIEWED" value={data.analytics.live_pull_request_count.toLocaleString()} detail="Unique account-owned public PRs; demo reviews excluded"/>
      <Metric label="FINDINGS IDENTIFIED" value={data.analytics.live_finding_count.toLocaleString()} detail="Exact lifetime count across account-owned live reviews; canonical issues are preferred over legacy findings"/>
      <Metric label="SUGGESTED FIXES" value={derived.fixCount.toLocaleString()} detail="Proposals in live PRs in the analyzed window"/>
      <Metric label="LEARNING EXERCISES" value={data.progress.exercises_completed.toLocaleString()} detail={`${data.progress.attempt_count} persisted answer attempts`}/>
    </div>
    {sampleIsPartial && <p className="profile-data-note">Finding, lifecycle, fix, and validation summaries cover the latest {data.reviews.length} of {data.analytics.total_reviews} stored review records. Older records are not counted. Demo reviews are excluded from developer activity.</p>}
    {data.analytics.total_reviews === 0 && <p className="workspace-empty">No live reviews are stored for this account yet. Demo reviews are excluded from your personal profile.</p>}
    <div className="analytics-grid">
      <section className="analytics-panel"><h2>Issue lifecycle</h2><p>Counts from the review history window; legacy findings without lifecycle state remain unclassified.</p><div className="analytics-row"><span>Resolved</span><strong>{derived.lifecycle.resolved}</strong></div><div className="analytics-row"><span>Open</span><strong>{derived.lifecycle.open}</strong></div><div className="analytics-row"><span>Dismissed</span><strong>{derived.lifecycle.dismissed}</strong></div><div className="analytics-row"><span>Unclassified legacy</span><strong>{derived.lifecycle.unclassified}</strong></div></section>
      <section className="analytics-panel"><h2>Finding categories</h2><p>Most frequent categories in the analyzed window.</p>{categoryRows.length ? categoryRows.slice(0, 8).map(([category, count]) => <div className="profile-bar-row" key={category}><span>{label(category)}</span><div className="profile-bar-track"><i style={{ width: `${(count / maxCategory) * 100}%` }}/></div><strong>{count}</strong></div>) : <p>No findings in the available review history.</p>}</section>
      <section className="analytics-panel"><h2>Findings over time</h2><p>Stored findings grouped by review month.</p>{derived.timeline.length ? <div className="profile-timeline" role="img" aria-label="Findings per month in recent review history">{derived.timeline.map(([month, count]) => <div key={month}><strong>{count}</strong><i style={{ height: `${Math.max(6, (count / maxMonth) * 100)}%` }}/><span>{formatMonth(month)}</span></div>)}</div> : <p>Monthly trends appear after dated reviews are available.</p>}</section>
      <section className="analytics-panel"><h2>Fix and validation outcomes</h2><p>Recorded developer decisions and deterministic reports from live PR reviews.</p>{[...derived.fixDecisions.entries()].map(([name, count]) => <div className="analytics-row" key={name}><span>Fixes {label(name)}</span><strong>{count}</strong></div>)}{(() => { const accepted = derived.fixDecisions.get('accepted') ?? 0; const rejected = derived.fixDecisions.get('rejected') ?? 0; const decided = accepted + rejected; return decided ? <p>Acceptance rate among recorded decisions: {Math.round((accepted / decided) * 100)}% ({accepted}/{decided}).</p> : null; })()}{[...derived.validationStatuses.entries()].map(([name, count]) => <div className="analytics-row" key={name}><span>Validation {label(name)}</span><strong>{count}</strong></div>)}{!derived.fixDecisions.size && !derived.validationStatuses.size && <p>No fix decisions or validation reports are recorded in the live PR review window.</p>}</section>
      <section className="analytics-panel"><h2>Learning progress</h2><p>{data.progress.attempt_count ? `${Math.round(data.progress.accuracy * 100)}% correct across ${data.progress.attempt_count} persisted attempts.` : 'No learning answers have been recorded yet.'}</p>{derived.learningRates.size > 0 && data.progress.attempt_count > data.learningHistory.attempts.length && <p>Category accuracy below uses the latest {data.learningHistory.attempts.length} attempts.</p>}{[...derived.learningRates.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([category, value]) => <div className="analytics-row" key={category}><span>{label(category)}</span><strong>{value.correct}/{value.attempts}</strong></div>)}<button type="button" className="secondary-button" onClick={onOpenLearning}><BookOpen size={15}/> Open Learning Lab</button></section>
      <section className="analytics-panel"><h2>Patterns and recommendations</h2><p>{data.insights.basis}</p>{categoryRows.slice(0, 4).map(([category, count]) => <p key={category}><strong>{label(category)}</strong>: {count} findings in this window{derived.categoryRates.get(category) ? ` · recent/earlier rate ${derived.categoryRates.get(category)!.recent.toFixed(2)}/${derived.categoryRates.get(category)!.earlier.toFixed(2)} per review` : ''}</p>)}{!categoryRows.length && <p>Recommendations will appear when review findings are available.</p>}</section>
    </div>
    <section className="workspace-panel profile-review-history"><div><h2>Recent review history</h2><p>Open a review to inspect reconciled findings, explanations, fixes, decisions, and validation attempts.</p></div><button type="button" className="secondary-button" onClick={onOpenHistory}>Full review history</button>{data.reviews.slice(0, 8).map((review) => <article key={review.review_id}><div><strong>{review.pull_request.repository_owner}/{review.pull_request.repository_name} · #{review.pull_request.pull_request_number}</strong><small>{review.pull_request.title} · {issuesOf(review).length} findings · {new Date(review.created_at).toLocaleDateString()}</small></div><button type="button" className="secondary-button" onClick={() => onOpenReview(review.review_id)}>Open review <ArrowUpRight size={14}/></button></article>)}{!data.reviews.length && <p>No review history is available yet.</p>}</section>
  </section>;
}

function Metric({ label: title, value, detail }: { label: string; value: string; detail: string }) {
  return <article className="analytics-panel"><small>{title}</small><strong className="analytics-value">{value}</strong><span>{detail}</span></article>;
}
