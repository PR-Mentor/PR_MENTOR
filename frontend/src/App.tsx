import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import {
  Activity, ArrowRight, Bookmark, BookOpen, CheckCircle2, ChevronRight, CircleAlert,
  Code2, ExternalLink, FileCode2,
  GitPullRequest, GraduationCap, History, LoaderCircle, Menu, Search,
  ShieldCheck, Sparkles, X,
} from 'lucide-react';
import {
  analyzePullRequest, deleteSavedPullRequest, getLearningExercises, getLearningHistory,
  getLearningProgress, getReview, getReviewDiff, getReviewHistory, getSavedPullRequests,
  savePullRequest, submitExercise,
} from './services/api';
import type {
  AnalysisMode, DiffFile, LearningExercise, LearningHistory, LearningProgress,
  LearningResult, ReviewFinding, ReviewResponse, SavedPullRequest, Severity,
} from './types/review';
import { useAuth } from './auth/AuthContext';
import LoginPage from './pages/LoginPage';

type Page = 'discover' | 'review' | 'history' | 'saved' | 'diff' | 'learning';

const nav: Array<{ id: Page; label: string; icon: typeof Search }> = [
  { id: 'discover', label: 'Discover', icon: Search },
  { id: 'history', label: 'Recent reviews', icon: History },
  { id: 'saved', label: 'Saved pull requests', icon: Bookmark },
  { id: 'learning', label: 'Learning lab', icon: GraduationCap },
];

function pageFromPath(path: string): Page {
  const value = path.replace(/^\//, '');
  return ['review', 'history', 'saved', 'diff', 'learning'].includes(value) ? value as Page : 'discover';
}

function formatDate(value?: string) {
  if (!value) return 'Date unavailable';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Date unavailable' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function pretty(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function App() {
  const auth = useAuth();
 codex/firebase-auth
  const location = useLocation();
  const isLoginRoute = location.pathname.replace(/\/$/, '') === '/login';
  if (isLoginRoute) return <LoginPage />;
  if (auth.loading) return <div className="app-auth-loading" role="status"><span className="brand-mark"><Code2 size={17}/><i/></span><LoaderCircle className="spin" size={18}/> Restoring your workspace…</div>;
  if (!auth.user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Workspace />;
}

function Workspace() {
  const auth = useAuth();
 main
  const navigateRouter = useNavigate();
  const location = useLocation();
  if (location.pathname.replace(/\/$/, '') === '/login') return <LoginPage />;
  if (auth.loading) return <div className="app-auth-loading" role="status"><span className="brand-mark"><Code2 size={17}/><i/></span><LoaderCircle className="spin" size={18}/> Restoring your workspace…</div>;
  const page = pageFromPath(location.pathname);
  const [mobileNav, setMobileNav] = useState(false);
  const [mobileViewport, setMobileViewport] = useState(() => window.matchMedia('(max-width: 760px)').matches);
  const mobileMenuRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const [url, setUrl] = useState('');
  const [mode, setMode] = useState<AnalysisMode>('demo');
  const [review, setReview] = useState<ReviewResponse | null>(null);
  const [reviewError, setReviewError] = useState('');
  const [reviewBusy, setReviewBusy] = useState(false);
  const [history, setHistory] = useState<ReviewResponse[]>([]);
  const [historyState, setHistoryState] = useState<'idle' | 'loading' | 'error' | 'ready'>('idle');
  const [historyError, setHistoryError] = useState('');
  const [saved, setSaved] = useState<SavedPullRequest[]>([]);
  const [savedState, setSavedState] = useState<'idle' | 'loading' | 'error' | 'ready'>('idle');
  const [savedError, setSavedError] = useState('');
  const [saveMessage, setSaveMessage] = useState('');
  const [diff, setDiff] = useState<DiffFile[]>([]);
  const [diffWarnings, setDiffWarnings] = useState<string[]>([]);
  const [diffState, setDiffState] = useState<'idle' | 'loading' | 'error' | 'ready'>('idle');
  const [diffError, setDiffError] = useState('');
  const [exercises, setExercises] = useState<LearningExercise[]>([]);
  const [learningState, setLearningState] = useState<'idle' | 'loading' | 'error' | 'ready'>('idle');
  const [learningError, setLearningError] = useState('');
  const [progress, setProgress] = useState<LearningProgress | null>(null);
  const [learningHistory, setLearningHistory] = useState<LearningHistory | null>(null);
  const [activeExercise, setActiveExercise] = useState('');
  const [answer, setAnswer] = useState('');
  const [answerResult, setAnswerResult] = useState<LearningResult | null>(null);
  const [answerError, setAnswerError] = useState('');
  const [answerBusy, setAnswerBusy] = useState(false);

  const go = useCallback((next: Page) => {
    navigateRouter(next === 'discover' ? '/' : `/${next}`);
    setMobileNav(false);
    if (mobileViewport) mobileMenuRef.current?.focus();
    window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }, [mobileViewport, navigateRouter]);

  useEffect(() => {
    const viewport = window.matchMedia('(max-width: 760px)');
    const updateViewport = () => setMobileViewport(viewport.matches);
    updateViewport();
    viewport.addEventListener('change', updateViewport);
    return () => viewport.removeEventListener('change', updateViewport);
  }, []);

  useEffect(() => {
    if (!mobileNav || !mobileViewport) return;
    sidebarRef.current?.querySelector<HTMLButtonElement>('nav button')?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMobileNav(false);
        mobileMenuRef.current?.focus();
      }
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [mobileNav, mobileViewport]);

  const runAnalysis = async (event: FormEvent) => {
    event.preventDefault();
    setReviewError('');
    if (!/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+\/?(?:[?#].*)?$/i.test(url.trim())) {
      setReviewError('Enter a public GitHub pull request URL, such as https://github.com/owner/repo/pull/42.');
      return;
    }
    setReviewBusy(true);
    setReview(null);
    try {
      const result = await analyzePullRequest(url.trim(), mode);
      setReview(result);
      go('review');
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : 'The review could not be loaded.');
    } finally {
      setReviewBusy(false);
    }
  };

  const openReview = async (reviewId: string) => {
    setReviewError('');
    setReviewBusy(true);
    try {
      const result = await getReview(reviewId);
      setReview(result);
      setUrl(result.pull_request.pull_request_url);
      setMode(result.analysis_mode);
      go('review');
    } catch (error) {
      setReview(null);
      setReviewError(error instanceof Error ? error.message : 'This review is unavailable.');
      go('review');
    } finally {
      setReviewBusy(false);
    }
  };

  const refreshSaved = useCallback(async () => {
    setSavedState('loading'); setSavedError('');
    try { setSaved(await getSavedPullRequests()); setSavedState('ready'); }
    catch (error) { setSavedError(error instanceof Error ? error.message : 'Saved pull requests are unavailable.'); setSavedState('error'); }
  }, []);

  useEffect(() => {
    if (page === 'history') {
      setHistoryState('loading'); setHistoryError('');
      getReviewHistory().then((items) => { setHistory(items); setHistoryState('ready'); })
        .catch((error: unknown) => { setHistoryError(error instanceof Error ? error.message : 'Review history is unavailable.'); setHistoryState('error'); });
    }
    if (page === 'saved') void refreshSaved();
    if (page === 'diff') {
      if (!review?.review_id) { setDiff([]); setDiffState('ready'); setDiffWarnings([]); return; }
      setDiffState('loading'); setDiffError('');
      getReviewDiff(review.review_id).then((result) => { setDiff(result.files); setDiffWarnings(result.warnings); setDiffState('ready'); })
        .catch((error: unknown) => { setDiffError(error instanceof Error ? error.message : 'The stored diff is unavailable.'); setDiffState('error'); });
    }
    if (page === 'learning') {
      setLearningState('loading'); setLearningError('');
      Promise.all([getLearningExercises(), getLearningProgress(), getLearningHistory()])
        .then(([items, nextProgress, attempts]) => {
          setExercises(items); setProgress(nextProgress); setLearningHistory(attempts);
          setActiveExercise((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id ?? '');
          setLearningState('ready');
        })
        .catch((error: unknown) => { setLearningError(error instanceof Error ? error.message : 'Learning exercises are unavailable.'); setLearningState('error'); });
    }
  }, [page, refreshSaved, review?.review_id]);

  const saveCurrent = async () => {
    if (!review?.pull_request.pull_request_url) return;
    setSaveMessage('');
    try {
      await savePullRequest(review.pull_request.pull_request_url);
      setSaveMessage('Pull request saved in backend storage.');
    } catch (error) { setSaveMessage(error instanceof Error ? error.message : 'Could not save this pull request.'); }
  };

  const removeSaved = async (item: SavedPullRequest) => {
    try { await deleteSavedPullRequest(item.saved_id); await refreshSaved(); }
    catch (error) { setSavedError(error instanceof Error ? error.message : 'Could not remove the saved pull request.'); }
  };

  const selectSaved = (item: SavedPullRequest) => { setUrl(item.url); setMode('live'); go('discover'); };

  const submitAnswer = async (event: FormEvent) => {
    event.preventDefault();
    if (!activeExercise || !answer.trim()) return;
    setAnswerBusy(true); setAnswerError(''); setAnswerResult(null);
    try {
      const result = await submitExercise(activeExercise, answer);
      setAnswerResult(result); setProgress(result.progress); setAnswer('');
      try { setLearningHistory(await getLearningHistory()); }
      catch { /* The accepted answer remains visible if only the history refresh fails. */ }
    } catch (error) { setAnswerError(error instanceof Error ? error.message : 'Your answer could not be submitted.'); }
    finally { setAnswerBusy(false); }
  };

  const activeExerciseData = exercises.find((item) => item.id === activeExercise);
  const activeTitle = useMemo(() => ({ discover: 'Discover', review: 'Review workspace', history: 'Recent reviews', saved: 'Saved pull requests', diff: 'Pull request diff', learning: 'Learning lab' }[page]), [page]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <button ref={mobileMenuRef} className="mobile-menu icon-button" type="button" onClick={() => setMobileNav((open) => !open)} aria-label={mobileNav ? 'Close navigation' : 'Open navigation'} aria-expanded={mobileNav} aria-controls="main-navigation">
          {mobileNav ? <X size={19}/> : <Menu size={19}/>}
        </button>
        <button className="brand" type="button" onClick={() => go('discover')} aria-label="PR Mentor home">
          <span className="brand-mark"><Code2 size={17}/><i/></span><span>PR <b>Mentor</b></span>
        </button>
        <div className="top-context"><span>PR REVIEW WORKSPACE</span><ChevronRight size={13}/><strong>{activeTitle}</strong></div>
        <a className="top-github" href="https://github.com/MAANDUBEY07/PR_MENTOR" target="_blank" rel="noreferrer"><Code2 size={16}/><span>Project</span><ExternalLink size={12}/></a>
      </header>
      {mobileNav && <button className="drawer-backdrop" type="button" onClick={() => { setMobileNav(false); mobileMenuRef.current?.focus(); }} aria-label="Close navigation"/>}
      <div className={`app-body ${mobileNav ? 'nav-open' : ''}`}>
        <aside ref={sidebarRef} id="main-navigation" className="sidebar" aria-label="Main navigation" aria-hidden={mobileViewport && !mobileNav} inert={mobileViewport && !mobileNav}>
          <div className="sidebar-label">WORKSPACE</div>
          <nav className="side-nav">
            {nav.map(({ id, label, icon: Icon }) => <button key={id} type="button" className={`side-link ${page === id ? 'selected' : ''}`} aria-current={page === id ? 'page' : undefined} onClick={() => go(id)}><Icon size={17}/><span>{label}</span>{id === 'history' && history.length > 0 && <small>{history.length}</small>}</button>)}
          </nav>
          <div className="sidebar-note"><div className="note-icon"><ShieldCheck size={16}/></div><strong>Evidence first</strong><p>AI suggestions need a human review. Static analyzer output is shown with its source.</p></div>
          <div className="sidebar-bottom">{auth.user?.avatar_url ? <img className="guest-avatar profile-avatar" src={auth.user.avatar_url} alt=""/> : <span className="guest-avatar">{auth.user ? auth.user.display_name.slice(0, 2).toUpperCase() : 'PM'}</span>}<span><strong>{auth.user?.display_name ?? 'Guest workspace'}</strong><small>{auth.user?.email ?? 'Public workspace'}</small></span>{auth.user ? <button className="logout-button" type="button" onClick={() => void auth.logout()} aria-label="Sign out">Sign out</button> : <button className="login-link" type="button" onClick={() => navigateRouter('/login')}>Sign in</button>}</div>
        </aside>

        <main className="main-content">
          {page === 'discover' && <section className="discover-page">
            <div className="hero-kicker"><span className="kicker-line"/> PULL REQUESTS, MADE CLEAR <span className="kicker-index">01 / REVIEW</span></div>
            <h1>Understand the Code.<br/><span>Review With Confidence.</span></h1>
            <p className="hero-description">A thoughtful workspace for reading changes, weighing evidence, and learning what deserves a closer look.</p>
            <div className="review-entry">
              <div className="entry-heading"><div><span className="section-index">01</span><strong>Start with a pull request</strong></div><span>PUBLIC GITHUB URL</span></div>
              <form onSubmit={runAnalysis}>
                <label className="url-label" htmlFor="pr-url">Pull request URL</label>
                <div className="url-field"><Code2 size={18}/><input id="pr-url" type="url" value={url} onChange={(event) => { setUrl(event.target.value); setReviewError(''); }} placeholder="https://github.com/owner/repository/pull/42" autoComplete="url"/><button className="analyze-button" type="submit" disabled={reviewBusy}>{reviewBusy ? <><LoaderCircle className="spin" size={16}/> Analyzing</> : <>Analyze PR <ArrowRight size={16}/></>}</button></div>
                <fieldset className="mode-select"><legend>Choose review mode <span>Required</span></legend><label className={`mode-option ${mode === 'demo' ? 'chosen' : ''}`}><input type="radio" name="mode" value="demo" checked={mode === 'demo'} onChange={() => setMode('demo')}/><span className="radio-mark"/><span><strong>Demo</strong><small>Fictional educational findings, clearly labeled</small></span><Sparkles size={15}/></label><label className={`mode-option ${mode === 'live' ? 'chosen' : ''}`}><input type="radio" name="mode" value="live" checked={mode === 'live'} onChange={() => setMode('live')}/><span className="radio-mark"/><span><strong>Live analysis</strong><small>Fetch public PR details and available Python checks</small></span><GitPullRequest size={15}/></label></fieldset>
                {reviewError && <div className="alert error-alert" role="alert"><CircleAlert size={16}/><span>{reviewError}</span></div>}
              </form>
              <div className="entry-foot"><span><ShieldCheck size={14}/> Submitted code is not executed.</span><span>Live analysis depends on GitHub access and installed analyzers.</span></div>
            </div>
            <div className="section-rule"><span>THE REVIEW PRACTICE</span><i/></div>
            <section className="principles" aria-label="Review principles">
              <article><span className="principle-no">01</span><div><h2>Read the change</h2><p>Review the actual pull request metadata and diff returned by the service.</p></div><FileCode2 size={19}/></article>
              <article><span className="principle-no">02</span><div><h2>Separate signal from certainty</h2><p>See whether a finding came from a static analyzer or an AI candidate needing review.</p></div><ShieldCheck size={19}/></article>
              <article><span className="principle-no">03</span><div><h2>Build the habit</h2><p>Practice with the learning exercises served by the backend.</p></div><BookOpen size={19}/></article>
            </section>
            <div className="notice-strip"><span className="notice-mark">i</span><p>Reviews, saved links, and exercise progress are stored by the backend. Retention depends on its configured storage.</p></div>
          </section>}

          {page === 'review' && <ReviewPage review={review} busy={reviewBusy} error={reviewError} onRetry={() => go('discover')} onSave={saveCurrent} saveMessage={saveMessage} onDiff={() => go('diff')}/>}
          {page === 'history' && <HistoryPage state={historyState} error={historyError} items={history} onOpen={openReview} onDiscover={() => go('discover')}/>}
          {page === 'saved' && <SavedPage state={savedState} error={savedError} items={saved} onRemove={removeSaved} onSelect={selectSaved} onRefresh={() => void refreshSaved()} onDiscover={() => go('discover')}/>}
          {page === 'diff' && <DiffPage review={review} state={diffState} error={diffError} files={diff} warnings={diffWarnings} onBack={() => go('review')} onDiscover={() => go('discover')}/>}
          {page === 'learning' && <LearningPage state={learningState} error={learningError} exercises={exercises} active={activeExerciseData} setActive={(id) => { setActiveExercise(id); setAnswer(''); setAnswerResult(null); setAnswerError(''); }} answer={answer} setAnswer={setAnswer} result={answerResult} answerError={answerError} busy={answerBusy} progress={progress} history={learningHistory} onSubmit={submitAnswer}/>}
          <footer className="footer"><span>PR Mentor <i>·</i> Review with care.</span><span>API data is shown as returned; unavailable services are labeled.</span></footer>
        </main>
      </div>
    </div>
  );
}

function ReviewPage({ review, busy, error, onRetry, onSave, saveMessage, onDiff }: { review: ReviewResponse | null; busy: boolean; error: string; onRetry: () => void; onSave: () => void; saveMessage: string; onDiff: () => void }) {
  if (busy) return <LoadingState title="Loading review" detail="Waiting for the PR Mentor API."/>;
  if (error && !review) return <ErrorState title="Review unavailable" message={error} action="Back to analysis" onAction={onRetry}/>;
  if (!review) return <EmptyState icon={<GitPullRequest size={22}/>} title="No review loaded" detail="Analyze a pull request in demo or live mode to open a review workspace." action="Analyze a PR" onAction={onRetry}/>;
  const pr = review.pull_request;
  const isDemo = review.is_demo || review.analysis_mode === 'demo';
  return <section className="content-page review-page">
    <div className="page-heading"><div><div className="eyebrow">REVIEW WORKSPACE <span className={`mode-badge ${isDemo ? 'demo' : 'live'}`}>{isDemo ? 'DEMO REVIEW' : 'LIVE REVIEW'}</span></div><h1>{pr.title || 'Pull request review'}</h1><p>{review.summary}</p></div><div className="heading-actions"><button type="button" className="secondary-button" onClick={onSave}><Bookmark size={15}/> Save PR</button><button type="button" className="primary-button" onClick={onDiff}>View diff <ArrowRight size={15}/></button></div></div>
    {saveMessage && <div className="inline-message" role="status">{saveMessage}</div>}
    <div className="pr-overview"><div className="repo-emblem"><Code2 size={21}/></div><div className="pr-ident"><a href={pr.pull_request_url} target="_blank" rel="noreferrer">{pr.repository_owner}/{pr.repository_name} <ExternalLink size={13}/></a><span>#{pr.pull_request_number} · {pr.state} · by {pr.author}</span></div><div className="pr-branches"><span>{pr.head_branch}</span><ArrowRight size={13}/><span>{pr.base_branch}</span></div><div className="pr-changes"><strong>+{pr.additions}</strong><strong>−{pr.deletions}</strong><span>{pr.changed_files} files</span></div></div>
    {isDemo && <div className="alert demo-alert"><Sparkles size={17}/><span><strong>Illustrative demo data.</strong> These findings do not describe the submitted pull request. No repository code was analyzed.</span></div>}
    {!isDemo && <div className="alert live-alert"><ShieldCheck size={17}/><span>Live PR metadata and available checks. Submitted code was not executed; analyzer and AI availability is listed below.</span></div>}
    {review.warnings.length > 0 && <div className="warning-list" aria-label="Review warnings">{review.warnings.map((warning, index) => <div key={`${index}-${warning}`}><CircleAlert size={15}/>{warning}</div>)}</div>}
    <div className="review-summary"><div><span>FINDINGS</span><strong>{review.findings.length}</strong></div><div><span>VERIFIED</span><strong>{review.verification_summary.verified ?? 0}</strong></div><div><span>NEEDS REVIEW</span><strong>{review.verification_summary.needs_review ?? 0}</strong></div><div><span>UNSUPPORTED</span><strong>{review.verification_summary.unsupported ?? 0}</strong></div></div>
    <div className="results-heading"><div><span className="section-index">02</span><div><h2>Findings & evidence</h2><p>{review.findings.length ? 'Each item retains its source and verification status.' : 'No findings were returned for this review.'}</p></div></div><span>{review.findings.length} TOTAL</span></div>
    {review.findings.length ? <div className="finding-list">{review.findings.map((finding) => <Finding key={finding.id} finding={finding}/>)}</div> : <div className="empty-inline"><CheckCircle2 size={18}/><span>No findings were returned. Check the tool results and warnings before treating this as a clean bill of health.</span></div>}
    <section className="tool-results"><div className="results-heading compact"><div><span className="section-index">03</span><div><h2>Analysis tools</h2><p>Backend-reported status; this is not a test-run summary.</p></div></div></div>{review.tool_results.length ? <div className="tool-table">{review.tool_results.map((tool, index) => <div className="tool-row" key={`${tool.tool}-${index}`}><span className="tool-name">{tool.tool}</span><span className={`tool-status status-${tool.status.replace(/[^a-z]/gi, '-').toLowerCase()}`}>{pretty(tool.status)}</span><span>{tool.reason ?? tool.message ?? (tool.findings ? `${tool.findings.length} diagnostics` : tool.executed === false ? 'Submitted code not executed' : 'Status returned by API')}</span></div>)}</div> : <div className="empty-inline">No tool status was returned.</div>}</section>
    <div className="review-meta"><span>Created {formatDate(review.created_at)}</span><span>{review.duration_ms} ms reported</span><span>Review ID <code>{review.review_id}</code></span></div>
  </section>;
}

function Finding({ finding }: { finding: ReviewFinding }) {
  const statusCopy: Record<string, string> = { verified: 'Verified against analyzer evidence', needs_review: 'Needs human review', unsupported: 'Unsupported by available evidence' };
  const sourceCopy: Record<string, string> = { static_analysis: 'Static analyzer', ai_candidate: 'AI candidate', test_evidence: 'Test evidence', combined: 'Combined evidence', demo: 'Demo sample' };
  return <article className="finding-card"><div className="finding-rail"><SeverityBadge severity={finding.severity}/><span className={`verification-badge verify-${finding.verification_status}`}><span/>{statusCopy[finding.verification_status] ?? pretty(finding.verification_status)}</span></div><div className="finding-main"><div className="finding-title-row"><h3>{finding.title}</h3><span className="source-label">{sourceCopy[finding.source] ?? pretty(finding.source)}</span></div><p className="finding-description">{finding.description}</p><div className="finding-location"><FileCode2 size={14}/><code>{finding.file_path}</code><span>{finding.line_start ? `Line ${finding.line_start}${finding.line_end && finding.line_end !== finding.line_start ? `–${finding.line_end}` : ''}` : 'Line unavailable'}</span><span>{pretty(finding.category)}</span><span>{Math.round(finding.confidence * 100)}% confidence</span></div>
    <details className="finding-evidence"><summary><ShieldCheck size={14}/> Evidence and context <ChevronRight size={14}/></summary><div><h4>Evidence</h4>{finding.evidence.length ? <ul>{finding.evidence.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : <p>No evidence was returned.</p>}<h4>Why it matters</h4><p>{finding.why_it_matters || 'No explanation was provided.'}</p><h4>Suggested next step</h4><p>{finding.suggested_fix || 'No suggested fix was provided.'}</p>{finding.learning_explanation && <><h4>Learning note</h4><p>{finding.learning_explanation}</p></>}{finding.limitations.length > 0 && <><h4>Limitations</h4><ul>{finding.limitations.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></>}</div></details>
  </div></article>;
}

function SeverityBadge({ severity }: { severity: Severity }) { return <span className={`severity-badge severity-${severity}`}><i/>{pretty(severity)}</span>; }

function HistoryPage({ state, error, items, onOpen, onDiscover }: { state: string; error: string; items: ReviewResponse[]; onOpen: (id: string) => void; onDiscover: () => void }) {
  return <section className="content-page"><PageHeading eyebrow="WORKSPACE / REVIEWS" title="Recent reviews" intro="Reviews stored by the configured backend."/>{state === 'loading' && <LoadingState title="Loading history" detail="Fetching recent reviews from the API."/>}{state === 'error' && <ErrorState title="History unavailable" message={error} action="Try analysis" onAction={onDiscover}/ >}{state === 'ready' && (items.length ? <div className="record-list">{items.map((item) => <button className="record-row" key={item.review_id} type="button" onClick={() => onOpen(item.review_id)}><span className="record-icon"><GitPullRequest size={17}/></span><span className="record-main"><strong>{item.pull_request.title}</strong><small>{item.pull_request.repository_owner}/{item.pull_request.repository_name} · #{item.pull_request.pull_request_number}</small></span><span className={`mode-badge ${item.is_demo ? 'demo' : 'live'}`}>{item.is_demo ? 'Demo' : 'Live'}</span><span className="record-time">{formatDate(item.created_at)}</span><ArrowRight size={15}/></button>)}</div> : <EmptyState icon={<History size={22}/>} title="No saved reviews" detail="The backend has not stored a review yet." action="Analyze a PR" onAction={onDiscover}/> )}</section>;
}

function SavedPage({ state, error, items, onRemove, onSelect, onRefresh, onDiscover }: { state: string; error: string; items: SavedPullRequest[]; onRemove: (item: SavedPullRequest) => void; onSelect: (item: SavedPullRequest) => void; onRefresh: () => void; onDiscover: () => void }) {
  return <section className="content-page"><PageHeading eyebrow="WORKSPACE / SAVED" title="Saved pull requests" intro="Links saved by the backend. Saving a link does not save review history."/><div className="page-heading-action"><button className="secondary-button" type="button" onClick={onRefresh}>Refresh <Activity size={14}/></button></div>{state === 'loading' && <LoadingState title="Loading saved links" detail="Fetching saved pull requests."/>}{state === 'error' && <ErrorState title="Saved links unavailable" message={error} action="Try again" onAction={onRefresh}/>}{state === 'ready' && (items.length ? <div className="record-list">{items.map((item) => <div className="record-row saved-row" key={item.saved_id}><span className="record-icon"><Bookmark size={17}/></span><button className="saved-url" type="button" onClick={() => onSelect(item)}>{item.url}</button><span className="temporary-label">{item.temporary ? 'Temporary' : 'Persistent'}</span><button className="remove-button" type="button" onClick={() => onRemove(item)} aria-label={`Remove ${item.url}`}><X size={16}/></button></div>)}</div> : <EmptyState icon={<Bookmark size={22}/>} title="No saved pull requests" detail="Save a pull request from its review workspace." action="Analyze a PR" onAction={onDiscover}/> )}</section>;
}

function DiffPage({ review, state, error, files, warnings, onBack, onDiscover }: { review: ReviewResponse | null; state: string; error: string; files: DiffFile[]; warnings: string[]; onBack: () => void; onDiscover: () => void }) {
  return <section className="content-page"><PageHeading eyebrow="REVIEW / DIFF" title="The changed lines" intro={review ? `${review.pull_request.repository_owner}/${review.pull_request.repository_name} · #${review.pull_request.pull_request_number}` : 'Diffs are available after a review is opened.'}/>{state === 'loading' && <LoadingState title="Loading diff" detail="Retrieving the stored diff from the API."/>}{state === 'error' && <ErrorState title="Diff unavailable" message={error} action="Back to review" onAction={onBack}/>}{state === 'ready' && !review && <EmptyState icon={<FileCode2 size={22}/>} title="No review selected" detail="Open a review first. Stored review diffs are available when the API returned them." action="Analyze a PR" onAction={onDiscover}/>}{state === 'ready' && review && (files.length ? <><div className="diff-toolbar"><span><FileCode2 size={15}/> {files.length} changed files returned</span><span>Diff patches may be omitted for binary or large files.</span></div>{warnings.map((warning, index) => <div className="alert warning-alert" key={`${index}-${warning}`}><CircleAlert size={15}/>{warning}</div>)}<div className="diff-files">{files.map((file) => <DiffFileView file={file} key={`${file.path}-${file.status}`}/>)}</div></> : <EmptyState icon={<FileCode2 size={22}/>} title="No diff lines available" detail="The API returned no patch lines for this review. A file may have an omitted patch, or the review may contain no diff data." action="Back to review" onAction={onBack}/> )}</section>;
}

function DiffFileView({ file }: { file: DiffFile }) {
  return <details className="diff-file" open><summary><span><FileCode2 size={15}/><code>{file.path}</code>{file.previous_path && <small>from {file.previous_path}</small>}</span><span className="diff-counts"><b>+{file.added}</b> <i>−{file.removed}</i> <em>{pretty(file.status)}</em></span></summary>{file.truncated && <div className="file-truncated"><CircleAlert size={14}/> GitHub did not provide a patch for this file.</div>}{file.hunks.map((hunk, index) => <section className="diff-hunk" key={`${hunk.header}-${index}`}><div className="hunk-header">{hunk.header}</div>{hunk.lines.map((line, lineIndex) => <div className={`diff-line ${line.kind}`} key={`${line.line}-${line.kind}-${lineIndex}`}><span>{line.line}</span><code>{line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '}&nbsp;{line.text}</code></div>)}</section>)}</details>;
}

function LearningPage({ state, error, exercises, active, setActive, answer, setAnswer, result, answerError, busy, progress, history, onSubmit }: { state: string; error: string; exercises: LearningExercise[]; active?: LearningExercise; setActive: (id: string) => void; answer: string; setAnswer: (value: string) => void; result: LearningResult | null; answerError: string; busy: boolean; progress: LearningProgress | null; history: LearningHistory | null; onSubmit: (event: FormEvent) => void }) {
  if (state === 'loading') return <section className="content-page"><PageHeading eyebrow="PRACTICE / LEARNING" title="Learning lab" intro="Exercises served by PR Mentor."/><LoadingState title="Loading exercises" detail="Fetching exercises and saved progress."/></section>;
  if (state === 'error') return <section className="content-page"><PageHeading eyebrow="PRACTICE / LEARNING" title="Learning lab" intro="Exercises served by PR Mentor."/><ErrorState title="Learning is unavailable" message={error} action="Try again" onAction={() => window.location.reload()}/></section>;
  if (!active) return <section className="content-page"><PageHeading eyebrow="PRACTICE / LEARNING" title="Learning lab" intro="Exercises served by PR Mentor."/><EmptyState icon={<BookOpen size={22}/>} title="No exercises available" detail="The backend returned an empty exercise list." action="Back to discover" onAction={() => window.location.assign('/')}/></section>;
  return <section className="content-page learning-page"><PageHeading eyebrow="PRACTICE / LEARNING" title="Learning lab" intro="Practice with exercises and progress stored by the backend."/><div className="learning-layout"><aside className="exercise-list"><div className="list-heading">EXERCISES <span>{exercises.length}</span></div>{exercises.map((exercise, index) => <button type="button" className={`exercise-link ${active.id === exercise.id ? 'active' : ''}`} key={exercise.id} onClick={() => setActive(exercise.id)}><span className="exercise-number">{String(index + 1).padStart(2, '0')}</span><span><strong>{exercise.title}</strong><small>{pretty(exercise.difficulty)} · {pretty(exercise.category)}</small></span><ChevronRight size={15}/></button>)}<div className="progress-box"><div><span>LEARNING ACCURACY</span><strong>{Math.round((progress?.accuracy ?? 0) * 100)}%</strong></div><div className="progress-track"><i style={{ width: `${Math.max(0, Math.min(100, (progress?.accuracy ?? 0) * 100))}%` }}/></div><small>{progress?.attempt_count ?? 0} attempts · {progress?.temporary ? 'temporary' : 'saved by backend'}</small></div></aside>
    <div className="exercise-content"><div className="exercise-meta"><span>{active.language.toUpperCase()} / {pretty(active.category)}</span><span>{pretty(active.difficulty)}</span></div><h2>{active.title}</h2><p>{active.description}</p><div className="exercise-instruction"><strong>{active.instructions}</strong></div><pre className="exercise-code"><code>{active.code_snippet}</code></pre>{active.hints.length > 0 && <details className="hint-details"><summary><CircleAlert size={14}/> Show a hint</summary><ul>{active.hints.map((hint, index) => <li key={`${index}-${hint}`}>{hint}</li>)}</ul></details>}
      <form onSubmit={onSubmit} className="exercise-form"><fieldset><legend>Choose your answer</legend>{active.options.map((option, index) => <label className={`answer-option ${answer === option ? 'selected' : ''}`} key={`${index}-${option}`}><input type="radio" name={`answer-${active.id}`} value={option} checked={answer === option} onChange={() => setAnswer(option)}/><span>{String.fromCharCode(65 + index)}</span>{option}</label>)}</fieldset>{answerError && <div className="alert error-alert" role="alert"><CircleAlert size={15}/>{answerError}</div>}<button className="primary-button" type="submit" disabled={!answer || busy}>{busy ? <><LoaderCircle className="spin" size={15}/> Checking</> : <>Submit answer <ArrowRight size={15}/></>}</button></form>
      {result && <div className={`answer-result ${result.correct ? 'correct' : 'incorrect'}`} role="status"><div><span>{result.correct ? <CheckCircle2 size={18}/> : <CircleAlert size={18}/>}</span><strong>{result.feedback}</strong></div><p>{result.explanation}</p><small>{result.points} points · progress updated by backend</small></div>}
      <div className="learning-foot"><div><Activity size={15}/><span>{progress?.exercises_completed ?? 0} submissions recorded</span></div><div><History size={15}/><span>{history?.attempts.length ?? 0} saved attempts</span></div></div>
    </div></div></section>;
}

function PageHeading({ eyebrow, title, intro }: { eyebrow: string; title: string; intro: string }) { return <div className="page-heading simple"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{intro}</p></div></div>; }

function LoadingState({ title, detail }: { title: string; detail: string }) { return <div className="state-panel loading-panel" role="status"><LoaderCircle size={21} className="spin"/><div><strong>{title}</strong><p>{detail}</p></div></div>; }

function ErrorState({ title, message, action, onAction }: { title: string; message: string; action: string; onAction: () => void }) { return <div className="state-panel error-panel" role="alert"><CircleAlert size={21}/><div><strong>{title}</strong><p>{message}</p><button type="button" className="text-action" onClick={onAction}>{action} <ArrowRight size={14}/></button></div></div>; }

function EmptyState({ icon, title, detail, action, onAction }: { icon: ReactNode; title: string; detail: string; action: string; onAction: () => void }) { return <div className="empty-panel"><span className="empty-icon">{icon}</span><strong>{title}</strong><p>{detail}</p><button type="button" className="secondary-button" onClick={onAction}>{action} <ArrowRight size={14}/></button></div>; }
