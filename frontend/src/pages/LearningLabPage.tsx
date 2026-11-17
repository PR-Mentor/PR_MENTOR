import { useEffect, useState, useCallback, useId } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Code2,
  ExternalLink,
  GraduationCap,
  History,
  Lightbulb,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import {
  getLearningDashboard,
  getLearningSpace,
  getSpaceConcepts,
  getSpaceExercises,
  getExerciseDraft,
  saveExerciseDraft,
  submitCodingExercise,
  getExerciseSubmissions,
} from '../services/api';
import type {
  CodingExercise,
  ExerciseDraft,
  ExerciseSubmission,
  LearningConcept,
  LearningLabDashboard,
  PRLearningSpace,
} from '../types/review';

interface Props {
  onOpenReview?: (reviewId: string) => void;
  onNavigateDiscover?: () => void;
}

function pretty(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDate(isoString?: string) {
  if (!isoString) return 'Recent';
  const d = new Date(isoString);
  return Number.isNaN(d.valueOf())
    ? 'Recent'
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'short' }).format(d);
}

export default function LearningLabPage({ onOpenReview, onNavigateDiscover }: Props) {
  // Navigation level: 'dashboard' (Level 1) | 'space' (Level 2) | 'exercise' (Level 3)
  const [level, setLevel] = useState<'dashboard' | 'space' | 'exercise'>('dashboard');

  // Level 1: Dashboard state
  const [dashboard, setDashboard] = useState<LearningLabDashboard | null>(null);
  const [dashLoading, setDashLoading] = useState(true);
  const [dashError, setDashError] = useState('');

  // Level 2: Selected Space state
  const [selectedSpaceId, setSelectedSpaceId] = useState<string | null>(null);
  const [selectedSpace, setSelectedSpace] = useState<PRLearningSpace | null>(null);
  const [concepts, setConcepts] = useState<LearningConcept[]>([]);
  const [spaceLoading, setSpaceLoading] = useState(false);
  const [spaceError, setSpaceError] = useState('');

  // Level 3: Selected Concept & Exercise state
  const [selectedConcept, setSelectedConcept] = useState<LearningConcept | null>(null);
  const [exercise, setExercise] = useState<CodingExercise | null>(null);
  const [code, setCode] = useState('');
  const [draft, setDraft] = useState<ExerciseDraft | null>(null);
  const [draftSaving, setDraftSaving] = useState(false);
  const [draftSaveMessage, setDraftSaveMessage] = useState('');
  const [submissions, setSubmissions] = useState<ExerciseSubmission[]>([]);
  const [showAllSubmissions, setShowAllSubmissions] = useState(false);
  const [latestSubmission, setLatestSubmission] = useState<ExerciseSubmission | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [revealedHints, setRevealedHints] = useState<number>(0);
  const [exerciseLoading, setExerciseLoading] = useState(false);
  const [exerciseError, setExerciseError] = useState('');

  // Unique IDs for accessibility
  const hintsHeadingId = useId();
  const criteriaHeadingId = useId();
  const historyHeadingId = useId();

  // Load Dashboard Data
  const loadDashboard = useCallback(async () => {
    setDashLoading(true);
    setDashError('');
    try {
      const data = await getLearningDashboard();
      setDashboard(data);
    } catch (err) {
      setDashError(err instanceof Error ? err.message : 'Unable to load Learning Lab dashboard.');
    } finally {
      setDashLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  // Navigate to Level 2: Space View
  const openSpace = async (spaceId: string) => {
    setSelectedSpaceId(spaceId);
    setLevel('space');
    setSpaceLoading(true);
    setSpaceError('');
    try {
      const [spaceData, conceptsData] = await Promise.all([
        getLearningSpace(spaceId),
        getSpaceConcepts(spaceId),
      ]);
      setSelectedSpace(spaceData);
      setConcepts(conceptsData);
    } catch (err) {
      setSpaceError(err instanceof Error ? err.message : 'Unable to load PR Learning Space.');
    } finally {
      setSpaceLoading(false);
    }
  };

  // Navigate to Level 3: Exercise View
  const openConceptExercise = async (space: PRLearningSpace, concept: LearningConcept) => {
    setSelectedSpace(space);
    setSelectedConcept(concept);
    setLevel('exercise');
    setExerciseLoading(true);
    setExerciseError('');
    setSubmitError('');
    setLatestSubmission(null);
    setRevealedHints(0);
    setDraftSaveMessage('');

    try {
      const exercises = await getSpaceExercises(space.space_id, concept.concept_id);
      if (!exercises || exercises.length === 0) {
        throw new Error('No coding exercise available for this concept.');
      }
      const ex = exercises[0];
      setExercise(ex);

      // Check for saved draft or submissions
      const [savedDraft, subs] = await Promise.all([
        getExerciseDraft(ex.exercise_id).catch(() => null),
        getExerciseSubmissions(ex.exercise_id).catch(() => []),
      ]);

      setDraft(savedDraft);
      setSubmissions(subs);
      if (subs.length > 0) {
        setLatestSubmission(subs[0]);
      }

      // Restore draft if available, otherwise set starter code
      if (savedDraft?.code) {
        setCode(savedDraft.code);
      } else {
        setCode(ex.starter_code);
      }
    } catch (err) {
      setExerciseError(err instanceof Error ? err.message : 'Unable to load coding exercise.');
    } finally {
      setExerciseLoading(false);
    }
  };

  // Direct open recommended exercise from dashboard
  const openRecommended = (ex: CodingExercise) => {
    const virtualSpace: PRLearningSpace = {
      space_id: ex.space_id,
      canonical_repo: 'practice/workspace',
      pr_number: 1,
      pr_title: 'Interactive Coding Practice',
      pr_url: '',
      latest_review_id: '',
      review_count: 1,
      review_snapshot_ids: [],
      primary_language: ex.language,
      languages: [ex.language],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    const virtualConcept: LearningConcept = {
      concept_id: ex.concept_id,
      space_id: ex.space_id,
      title: ex.title,
      summary: ex.description,
      why_it_matters: ex.explanation,
      category: ex.category,
      language: ex.language,
      canonical_issue_ids: [],
      exercise_ids: [ex.exercise_id],
      order_index: 0,
      status: 'in_progress',
    };
    void openConceptExercise(virtualSpace, virtualConcept);
  };

  // Actions in Exercise Workspace
  const handleSaveDraft = async () => {
    if (!exercise || !code.trim()) return;
    setDraftSaving(true);
    setDraftSaveMessage('');
    try {
      const saved = await saveExerciseDraft(exercise.exercise_id, code);
      setDraft(saved);
      setDraftSaveMessage('Draft saved successfully.');
      setTimeout(() => setDraftSaveMessage(''), 3000);
    } catch (err) {
      setDraftSaveMessage('Failed to save draft.');
    } finally {
      setDraftSaving(false);
    }
  };

  const handleResetCode = () => {
    if (!exercise) return;
    if (window.confirm('Reset code back to original starter code? Any unsaved edits will be lost.')) {
      setCode(exercise.starter_code);
      setDraftSaveMessage('Code reset to starter code.');
      setTimeout(() => setDraftSaveMessage(''), 3000);
    }
  };

  const handleSubmitCode = async () => {
    if (!exercise || !code.trim() || submitting) return;
    setSubmitting(true);
    setSubmitError('');
    try {
      const sub = await submitCodingExercise(exercise.exercise_id, code);
      setLatestSubmission(sub);
      setSubmissions((prev) => [sub, ...prev]);

      // If passed, refresh dashboard in background
      if (sub.passed) {
        void loadDashboard();
      }
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Submission failed. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  const revealNextHint = () => {
    if (!exercise) return;
    if (revealedHints < exercise.hints.length) {
      setRevealedHints((prev) => prev + 1);
    }
  };

  // Back navigation handlers
  const handleBackToDashboard = () => {
    setLevel('dashboard');
    setSelectedSpaceId(null);
    void loadDashboard();
  };

  const handleBackToSpace = () => {
    if (selectedSpace) {
      setLevel('space');
    } else {
      handleBackToDashboard();
    }
  };

  // --------------------------------------------------------------------------
  // RENDER LEVEL 3: CONCEPT & PRACTICAL CODING EXERCISE WORKSPACE
  // --------------------------------------------------------------------------
  if (level === 'exercise') {
    return (
      <section className="content-page learning-workspace-page">
        {/* Breadcrumb Navigation */}
        <nav className="learning-breadcrumb" aria-label="Breadcrumbs">
          <button type="button" className="breadcrumb-link" onClick={handleBackToDashboard}>
            <GraduationCap size={14} /> Learning Lab
          </button>
          <ChevronRight size={13} className="breadcrumb-sep" />
          <button type="button" className="breadcrumb-link" onClick={handleBackToSpace}>
            {selectedSpace?.canonical_repo || 'Learning Space'} #{selectedSpace?.pr_number}
          </button>
          <ChevronRight size={13} className="breadcrumb-sep" />
          <span className="breadcrumb-current">{exercise?.title || 'Coding Exercise'}</span>
        </nav>

        {exerciseLoading && (
          <div className="state-panel loading-panel" role="status">
            <LoaderCircle size={21} className="spin" />
            <div>
              <strong>Loading coding exercise</strong>
              <p>Preparing the concept workspace and starter code…</p>
            </div>
          </div>
        )}

        {exerciseError && (
          <div className="state-panel error-panel" role="alert">
            <CircleAlert size={21} />
            <div>
              <strong>Exercise Unavailable</strong>
              <p>{exerciseError}</p>
              <button type="button" className="text-action" onClick={handleBackToSpace}>
                <ArrowLeft size={14} /> Back to Space
              </button>
            </div>
          </div>
        )}

        {exercise && !exerciseLoading && (
          <div className="coding-workspace-layout">
            {/* Left Column: Concept Context & Instructions */}
            <div className="workspace-concept-pane">
              <div className="concept-header">
                <div className="concept-tags">
                  <span className={`lang-badge lang-${exercise.language}`}>{exercise.language.toUpperCase()}</span>
                  <span className="category-badge">{pretty(exercise.category)}</span>
                  <span className={`difficulty-badge diff-${exercise.difficulty}`}>{pretty(exercise.difficulty)}</span>
                </div>
                <h2>{exercise.title}</h2>
                <p className="concept-lead">{exercise.description}</p>
              </div>

              {selectedConcept?.why_it_matters && (
                <div className="why-it-matters-card">
                  <div className="card-title">
                    <Lightbulb size={16} /> Why This Matters In Real Code
                  </div>
                  <p>{selectedConcept.why_it_matters}</p>
                </div>
              )}

              <div className="instructions-card">
                <h3>Instructions</h3>
                <p className="instruction-text">{exercise.instructions}</p>
                {exercise.expected_behavior && (
                  <div className="expected-behavior">
                    <strong>Expected Behavior:</strong> {exercise.expected_behavior}
                  </div>
                )}
              </div>

              {exercise.completion_criteria && exercise.completion_criteria.length > 0 && (
                <div className="criteria-card" aria-labelledby={criteriaHeadingId}>
                  <h4 id={criteriaHeadingId}>Completion Criteria</h4>
                  <ul>
                    {exercise.completion_criteria.map((crit, idx) => (
                      <li key={idx}>
                        <CheckCircle2 size={13} /> <span>{crit}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Step-by-Step Hints */}
              {exercise.hints && exercise.hints.length > 0 && (
                <div className="hints-section" aria-labelledby={hintsHeadingId}>
                  <div className="hints-header">
                    <span id={hintsHeadingId} className="hints-title"><Lightbulb size={14} /> Hints ({revealedHints}/{exercise.hints.length})</span>
                    {revealedHints < exercise.hints.length && (
                      <button type="button" className="text-hint-btn" onClick={revealNextHint}>
                        Reveal next hint
                      </button>
                    )}
                  </div>
                  <div className="hints-list">
                    {exercise.hints.slice(0, revealedHints).map((hint, idx) => (
                      <div key={idx} className="hint-card">
                        <span className="hint-number">Hint {idx + 1}:</span>
                        <span>{hint}</span>
                      </div>
                    ))}
                    {revealedHints === 0 && (
                      <p className="hint-prompt">Need guidance? Reveal a hint to steer your defense without giving away the full solution.</p>
                    )}
                  </div>
                </div>
              )}

              {/* Submissions History */}
              {submissions.length > 0 && (
                <div className="history-section" aria-labelledby={historyHeadingId}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                    <h4 id={historyHeadingId} style={{ margin: 0 }}><History size={14} /> Attempt History ({submissions.length})</h4>
                    {submissions.length > 5 && (
                      <button
                        type="button"
                        className="text-action"
                        onClick={() => setShowAllSubmissions(!showAllSubmissions)}
                        style={{ fontSize: '0.8rem', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-primary, #6366f1)' }}
                      >
                        {showAllSubmissions ? 'Show recent 5' : `Show all ${submissions.length}`}
                      </button>
                    )}
                  </div>
                  <div className="history-items">
                    {(showAllSubmissions ? submissions : submissions.slice(0, 5)).map((sub) => (
                      <div key={sub.submission_id} className={`history-item ${sub.passed ? 'passed' : 'failed'}`}>
                        <div className="history-status">
                          {sub.passed ? <CheckCircle2 size={14} /> : <CircleAlert size={14} />}
                          <span>{sub.passed ? 'Passed' : 'Failed'}</span>
                        </div>
                        <span className="history-time">{formatDate(sub.submitted_at)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Right Column: Interactive Code Editor & Evaluation Panel */}
            <div className="workspace-editor-pane">
              <div className="editor-toolbar">
                <div className="editor-title">
                  <Code2 size={15} />
                  <span>Interactive Editor</span>
                  {draft && <span className="draft-badge">Draft restored</span>}
                </div>
                <div className="editor-actions">
                  <button
                    type="button"
                    className="secondary-button compact-btn"
                    onClick={handleResetCode}
                    title="Reset to starter code"
                  >
                    <RotateCcw size={13} /> Reset
                  </button>
                  <button
                    type="button"
                    className="secondary-button compact-btn"
                    onClick={handleSaveDraft}
                    disabled={draftSaving}
                    title="Save current progress"
                  >
                    {draftSaving ? <LoaderCircle size={13} className="spin" /> : <Save size={13} />}
                    <span>{draftSaving ? 'Saving…' : 'Save Draft'}</span>
                  </button>
                  <button
                    type="button"
                    className="primary-button compact-btn"
                    onClick={handleSubmitCode}
                    disabled={submitting || !code.trim()}
                  >
                    {submitting ? (
                      <>
                        <LoaderCircle size={13} className="spin" /> Verifying…
                      </>
                    ) : (
                      <>
                        <span>Submit Code</span> <ArrowRight size={13} />
                      </>
                    )}
                  </button>
                </div>
              </div>

              {draftSaveMessage && <div className="inline-message info-message">{draftSaveMessage}</div>}
              {submitError && <div className="alert error-alert" role="alert">{submitError}</div>}

              {/* Code Editor Area */}
              <div className="editor-container">
                <textarea
                  className="code-textarea"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="Write your safe implementation here…"
                  spellCheck={false}
                  autoCapitalize="off"
                  autoComplete="off"
                  autoCorrect="off"
                />
              </div>

              {/* Evaluation & Feedback Panel */}
              <div className="evaluation-panel">
                <div className="eval-policy-notice">
                  <ShieldCheck size={14} />
                  <span>
                    <strong>Truthful Static Verification:</strong> Syntactic &amp; defensive pattern analysis. Runtime container execution is disabled by policy.
                  </span>
                </div>

                {latestSubmission && (
                  <div
                    className={`submission-feedback-card ${
                      latestSubmission.passed ? 'outcome-passed' : 'outcome-failed'
                    }`}
                    role="status"
                  >
                    <div className="feedback-head">
                      {latestSubmission.passed ? (
                        <>
                          <CheckCircle2 size={18} className="feedback-icon passed-icon" />
                          <strong>Verification Passed!</strong>
                        </>
                      ) : (
                        <>
                          <CircleAlert size={18} className="feedback-icon failed-icon" />
                          <strong>Verification Needs Adjustment</strong>
                        </>
                      )}
                    </div>
                    <p className="feedback-body">{latestSubmission.feedback}</p>
                    <div className="feedback-meta">
                      <span>Evaluated at {formatDate(latestSubmission.submitted_at)}</span>
                      {latestSubmission.passed && <span className="points-awarded">+{exercise.points} points awarded</span>}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </section>
    );
  }

  // --------------------------------------------------------------------------
  // RENDER LEVEL 2: PR LEARNING SPACE VIEW
  // --------------------------------------------------------------------------
  if (level === 'space' && selectedSpace) {
    return (
      <section className="content-page learning-space-page">
        {/* Breadcrumb Navigation */}
        <nav className="learning-breadcrumb" aria-label="Breadcrumbs">
          <button type="button" className="breadcrumb-link" onClick={handleBackToDashboard}>
            <GraduationCap size={14} /> Learning Lab
          </button>
          <ChevronRight size={13} className="breadcrumb-sep" />
          <span className="breadcrumb-current">
            {selectedSpace.canonical_repo} #{selectedSpace.pr_number}
          </span>
        </nav>

        {spaceLoading && (
          <div className="state-panel loading-panel" role="status">
            <LoaderCircle size={21} className="spin" />
            <div>
              <strong>Loading PR Learning Space</strong>
              <p>Gathering review concepts and coding exercises…</p>
            </div>
          </div>
        )}

        {spaceError && (
          <div className="state-panel error-panel" role="alert">
            <CircleAlert size={21} />
            <div>
              <strong>Learning Space Unavailable</strong>
              <p>{spaceError}</p>
              <button type="button" className="text-action" onClick={handleBackToDashboard}>
                <ArrowLeft size={14} /> Back to Dashboard
              </button>
            </div>
          </div>
        )}

        {!spaceLoading && (
          <>
            {/* PR Space Context Banner */}
            <div className="space-context-card">
              <div className="space-headline">
                <div className="space-ident">
                  <div className="repo-badge">
                    <Code2 size={20} />
                  </div>
                  <div>
                    <div className="eyebrow">PR LEARNING SPACE</div>
                    <h1>
                      {selectedSpace.canonical_repo} #{selectedSpace.pr_number}
                    </h1>
                    <p className="pr-title-text">{selectedSpace.pr_title}</p>
                  </div>
                </div>
                <div className="space-badges">
                  {selectedSpace.languages.map((l) => (
                    <span key={l} className={`lang-badge lang-${l}`}>
                      {l.toUpperCase()}
                    </span>
                  ))}
                  <span className="reviews-badge">
                    {selectedSpace.review_count} {selectedSpace.review_count === 1 ? 'Review' : 'Reviews'} Synced
                  </span>
                </div>
              </div>

              <div className="space-meta-strip">
                {selectedSpace.pr_url && (
                  <a
                    href={selectedSpace.pr_url}
                    target="_blank"
                    rel="noreferrer"
                    className="external-link-btn"
                  >
                    <span>View Pull Request on GitHub</span> <ExternalLink size={13} />
                  </a>
                )}
                {selectedSpace.latest_review_id && onOpenReview && (
                  <button
                    type="button"
                    className="secondary-button compact-btn"
                    onClick={() => onOpenReview(selectedSpace.latest_review_id)}
                  >
                    Open Review Workspace <ArrowRight size={13} />
                  </button>
                )}
              </div>
            </div>

            {/* Concepts Section */}
            <div className="concepts-container">
              <div className="section-title-row">
                <div>
                  <h2>Derived Learning Concepts</h2>
                  <p>
                    Educational concepts extracted directly from findings in this pull request review.
                  </p>
                </div>
                <span className="concept-count-label">{concepts.length} CONCEPTS</span>
              </div>

              {concepts.length === 0 ? (
                <div className="empty-panel">
                  <BookOpen size={24} className="empty-icon" />
                  <strong>No Concepts Generated Yet</strong>
                  <p>Concepts are generated as reviews for this pull request are analyzed.</p>
                </div>
              ) : (
                <div className="concept-grid">
                  {concepts.map((concept, index) => (
                    <div key={concept.concept_id} className="concept-card">
                      <div className="concept-card-head">
                        <span className="concept-order">CONCEPT {String(index + 1).padStart(2, '0')}</span>
                        <span className={`status-pill status-${concept.status}`}>
                          {pretty(concept.status)}
                        </span>
                      </div>
                      <h3>{concept.title}</h3>
                      <p className="concept-summary">{concept.summary}</p>
                      {concept.why_it_matters && (
                        <div className="concept-why">
                          <strong>Why it matters:</strong> {concept.why_it_matters}
                        </div>
                      )}
                      <div className="concept-footer">
                        <span className={`lang-badge lang-${concept.language}`}>
                          {concept.language.toUpperCase()}
                        </span>
                        <button
                          type="button"
                          className="primary-button compact-btn"
                          onClick={() => void openConceptExercise(selectedSpace, concept)}
                        >
                          <span>Practice Exercise</span> <ArrowRight size={13} />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </section>
    );
  }

  // --------------------------------------------------------------------------
  // RENDER LEVEL 1: LEARNING LAB DASHBOARD
  // --------------------------------------------------------------------------
  return (
    <section className="content-page learning-dashboard-page">
      {/* Page Header */}
      <div className="page-heading simple">
        <div>
          <div className="eyebrow">PRACTICAL CODING EDUCATION</div>
          <h1>Learning Lab</h1>
          <p>
            Master defensive coding concepts grounded in real findings from your pull request reviews.
          </p>
        </div>
        <div className="heading-actions">
          <button
            type="button"
            className="secondary-button"
            onClick={loadDashboard}
            disabled={dashLoading}
          >
            <RefreshCw size={14} className={dashLoading ? 'spin' : ''} /> Refresh
          </button>
        </div>
      </div>

      {dashLoading && (
        <div className="state-panel loading-panel" role="status">
          <LoaderCircle size={21} className="spin" />
          <div>
            <strong>Loading Learning Lab</strong>
            <p>Aggregating pull request spaces and learning progress…</p>
          </div>
        </div>
      )}

      {dashError && (
        <div className="state-panel error-panel" role="alert">
          <CircleAlert size={21} />
          <div>
            <strong>Dashboard Unavailable</strong>
            <p>{dashError}</p>
            <button type="button" className="text-action" onClick={loadDashboard}>
              <RefreshCw size={14} /> Try again
            </button>
          </div>
        </div>
      )}

      {dashboard && !dashLoading && (
        <>
          {/* Metrics Overview Cards */}
          <div className="analytics-cards">
            <article className="analytics-panel">
              <small>PR LEARNING SPACES</small>
              <strong className="analytics-value">{dashboard.total_spaces}</strong>
              <span>Connected pull request repositories</span>
            </article>
            <article className="analytics-panel">
              <small>CONCEPTS DERIVED</small>
              <strong className="analytics-value">{dashboard.total_concepts}</strong>
              <span>Security &amp; quality topics extracted</span>
            </article>
            <article className="analytics-panel">
              <small>EXERCISES COMPLETED</small>
              <strong className="analytics-value">{dashboard.completed_exercises}</strong>
              <span>Verified defensive solutions submitted</span>
            </article>
            <article className="analytics-panel">
              <small>CONCEPTS MASTERED</small>
              <strong className="analytics-value">{dashboard.mastered_concepts}</strong>
              <span>Fully resolved topic areas</span>
            </article>
          </div>

          {/* Main Grid: Spaces & Activity */}
          <div className="learning-dashboard-grid">
            {/* PR Learning Spaces List */}
            <div className="dashboard-section spaces-section">
              <div className="section-head">
                <div className="section-head-title">
                  <Code2 size={17} />
                  <h2>Your PR Learning Spaces</h2>
                </div>
                <span>{dashboard.active_spaces.length} ACTIVE</span>
              </div>

              {dashboard.active_spaces.length === 0 ? (
                <div className="empty-panel">
                  <GraduationCap size={24} className="empty-icon" />
                  <strong>No PR Learning Spaces Yet</strong>
                  <p>
                    PR Mentor automatically creates a learning space whenever you analyze a pull request.
                  </p>
                  {onNavigateDiscover && (
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={onNavigateDiscover}
                    >
                      <Sparkles size={14} /> Analyze a Pull Request
                    </button>
                  )}
                </div>
              ) : (
                <div className="space-card-list">
                  {dashboard.active_spaces.map((sp) => (
                    <div key={sp.space_id} className="space-item-card">
                      <div className="space-item-head">
                        <div className="space-repo-title">
                          <strong>
                            {sp.canonical_repo} #{sp.pr_number}
                          </strong>
                          <span className="space-pr-title">{sp.pr_title}</span>
                        </div>
                        <div className="space-lang-pills">
                          {sp.languages.map((l) => (
                            <span key={l} className={`lang-badge lang-${l}`}>
                              {l.toUpperCase()}
                            </span>
                          ))}
                        </div>
                      </div>
                      <div className="space-item-footer">
                        <span className="space-sync-info">
                          {sp.review_count} {sp.review_count === 1 ? 'review' : 'reviews'} synchronized
                        </span>
                        <button
                          type="button"
                          className="primary-button compact-btn"
                          onClick={() => void openSpace(sp.space_id)}
                        >
                          <span>Open Learning Space</span> <ArrowRight size={13} />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Sidebar: Recommended Practice & Recent Submissions */}
            <div className="dashboard-sidebar">
              {/* Recommended Coding Exercises */}
              {dashboard.recommended_exercises && dashboard.recommended_exercises.length > 0 && (
                <div className="dashboard-section">
                  <div className="section-head">
                    <div className="section-head-title">
                      <Sparkles size={16} />
                      <h3>Recommended Practice</h3>
                    </div>
                  </div>
                  <div className="recommended-list">
                    {dashboard.recommended_exercises.map((rec) => (
                      <div key={rec.exercise_id} className="rec-exercise-card">
                        <div className="rec-card-top">
                          <span className={`lang-badge lang-${rec.language}`}>
                            {rec.language.toUpperCase()}
                          </span>
                          <span className="rec-category">{pretty(rec.category)}</span>
                        </div>
                        <h4>{rec.title}</h4>
                        <p>{rec.description}</p>
                        <button
                          type="button"
                          className="text-action"
                          onClick={() => openRecommended(rec)}
                        >
                          Start Exercise <ArrowRight size={13} />
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Recent Activity */}
              <div className="dashboard-section">
                <div className="section-head">
                  <div className="section-head-title">
                    <History size={16} />
                    <h3>Recent Submissions</h3>
                  </div>
                </div>
                {dashboard.recent_submissions.length === 0 ? (
                  <p className="empty-subtext">No submissions recorded yet. Pick an exercise to practice!</p>
                ) : (
                  <div className="recent-subs-list">
                    {dashboard.recent_submissions.map((sub) => (
                      <div
                        key={sub.submission_id}
                        className={`recent-sub-item ${sub.passed ? 'passed' : 'failed'}`}
                      >
                        <div className="sub-item-header">
                          <div className="sub-item-status">
                            {sub.passed ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}
                            <span>{sub.passed ? 'Passed' : 'Needs Work'}</span>
                          </div>
                          <span className="sub-item-time">{formatDate(sub.submitted_at)}</span>
                        </div>
                        <p className="sub-item-feedback">{sub.feedback}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
