import type { FormEvent } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowRight, BookOpen, Code2, GitPullRequest,
  ScanSearch, ShieldCheck, Sparkles, WandSparkles,
} from 'lucide-react';
import type { AnalysisMode } from '../types/review';

type LandingPageProps = {
  url: string;
  setUrl: (value: string) => void;
  mode: AnalysisMode;
  setMode: (value: AnalysisMode) => void;
  busy: boolean;
  error: string;
  onSubmit: (event: FormEvent) => void;
};

const steps = [
  { number: '01', title: 'Bring a pull request', copy: 'Start with a public GitHub PR link and choose a demo or live review.' },
  { number: '02', title: 'Read the evidence', copy: 'See changed lines alongside the available static and AI review signals.' },
  { number: '03', title: 'Build your instincts', copy: 'Use clear explanations and practice exercises to carry each lesson forward.' },
];

const capabilities = [
  { icon: ScanSearch, title: 'Code quality', tag: 'RUFF', copy: 'Surface Python lint findings with their source, location, and context.' },
  { icon: ShieldCheck, title: 'Security signals', tag: 'BANDIT', copy: 'Spot security patterns and understand why each one deserves a look.' },
  { icon: WandSparkles, title: 'AI review', tag: 'CANDIDATES', copy: 'Get careful suggestions that stay labeled for human verification.' },
  { icon: BookOpen, title: 'Learning lab', tag: 'PRACTICE', copy: 'Turn review concepts into small exercises and track progress over time.' },
];

export default function LandingPage({ url, setUrl, mode, setMode, busy, error, onSubmit }: LandingPageProps) {
  return (
    <div className="landing-page">
      <section className="landing-hero" aria-labelledby="hero-title">
        <div className="hero-copy">
          <h1 id="hero-title">Review the code.<br/><span>Grow the engineer.</span></h1>
          <p className="landing-lede">PR Mentor makes pull request feedback easier to understand, verify, and learn from.</p>
          <div className="hero-actions">
            <a className="landing-cta" href="#start-review">Start reviewing <ArrowRight size={17}/></a>
            <a className="landing-text-link" href="#how-it-works">See how it works <ArrowRight size={15}/></a>
          </div>
        </div>

        <div className="preview-wrap" aria-label="Illustrative pull request review preview">
          <div className="review-preview">
            <div className="preview-topline"><span><i/> REVIEW PREVIEW</span><span>ILLUSTRATIVE</span></div>
            <div className="preview-pr"><span className="preview-icon"><GitPullRequest size={17}/></span><div><strong>Handle empty input</strong><small>src/summary.py · pull request #24</small></div><span className="preview-open">OPEN</span></div>
            <div className="preview-code" aria-label="Illustrative code diff">
              <div className="code-line dim"><span>18</span><code>def summarize(items):</code></div>
              <div className="code-line removed"><span>19</span><code>−&nbsp; return sum(items) / len(items)</code></div>
              <div className="code-line added"><span>19</span><code>+&nbsp; if not items:</code></div>
              <div className="code-line added"><span>20</span><code>+&nbsp;&nbsp;&nbsp;&nbsp; return 0</code></div>
              <div className="code-line dim"><span>21</span><code>+&nbsp; return sum(items) / len(items)</code></div>
            </div>
            <div className="preview-finding"><div className="preview-finding-head"><span className="preview-severity"><i/> REVIEW NOTE</span><span className="preview-source"><Sparkles size={12}/> AI candidate</span></div><strong>What happens when the list is empty?</strong><p>Check that the new behavior matches the function’s intended contract.</p><div className="preview-evidence"><Code2 size={13}/> Evidence linked to the change</div></div>
          </div>
        </div>
      </section>

      <section className="start-panel" id="start-review" aria-labelledby="start-title">
        <div className="start-intro"><div><h2 id="start-title">Review a pull request</h2><p>Paste a pull request URL. Choose a demo to explore the workspace or a live review to analyze a PR the backend can access.</p></div></div>
        <form className="landing-form" onSubmit={onSubmit}>
          <label htmlFor="pr-url">GitHub pull request URL</label>
          <div className="landing-input-row"><GitPullRequest size={18}/><input id="pr-url" type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://github.com/owner/repository/pull/42" autoComplete="url"/><button className="analyze-button" type="submit" disabled={busy}>{busy ? <><span className="spin"/> Reviewing</> : <>Analyze PR <ArrowRight size={16}/></>}</button></div>
          <fieldset className="landing-mode-select"><legend>Review mode</legend><label className={mode === 'demo' ? 'is-selected' : ''}><input type="radio" name="mode" value="demo" checked={mode === 'demo'} onChange={() => setMode('demo')}/><span><strong>Demo</strong><small>Explore illustrative learning findings</small></span></label><label className={mode === 'live' ? 'is-selected' : ''}><input type="radio" name="mode" value="live" checked={mode === 'live'} onChange={() => setMode('live')}/><span><strong>Live analysis</strong><small>Review available PR evidence</small></span></label></fieldset>
          {error && <div className="alert error-alert" role="alert">{error}</div>}
          <p className="form-note"><ShieldCheck size={14}/> Submitted repository code is not executed by PR Mentor.</p>
        </form>
      </section>

      <section className="how-section" id="how-it-works" aria-labelledby="how-title">
        <div className="section-heading"><div><h2 id="how-title">From pull request<br/>to better judgment.</h2></div><p>Every signal comes with enough context to help you decide what to inspect next.</p></div>
        <div className="steps-grid">{steps.map((step) => <article className="step-card" key={step.number}><span>{step.number}</span><h3>{step.title}</h3><p>{step.copy}</p></article>)}</div>
      </section>

      <section className="capabilities-section" aria-labelledby="capabilities-title">
        <div className="section-heading"><div><h2 id="capabilities-title">From code to learning</h2></div><p>Compare code quality and security signals, review AI suggestions, then practice what you learn.</p></div>
        <div className="capability-grid">{capabilities.map(({ icon: Icon, title, tag, copy }, index) => <article className={`capability-card capability-${index + 1}`} key={title}><div className="capability-top"><span><Icon size={19}/></span><small>{tag}</small></div><h3>{title}</h3><p>{copy}</p></article>)}</div>
      </section>

      <section className="learning-promise" aria-labelledby="promise-title"><div><h2 id="promise-title">Find the issue.<br/><span>Understand why.</span></h2></div><p className="promise-copy">A finding can point to a problem. A good review helps you see the reasoning, check the evidence, and make a stronger decision next time.</p><Link to="/learning" className="landing-text-link">Explore the Learning Lab <ArrowRight size={15}/></Link></section>

      <section className="landing-bottom-cta"><div><h2>Make your next review<br/>a little more meaningful.</h2></div><a className="landing-cta" href="#start-review">Start reviewing <ArrowRight size={17}/></a></section>
    </div>
  );
}
