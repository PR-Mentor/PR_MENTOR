import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight, Check, Code2, LoaderCircle, ShieldCheck, Sparkles } from 'lucide-react';
import { useAuth } from '../auth/AuthContext';

export default function LoginPage() {
  const { user, loading, error: authError, configured, configurationError, signInWithGithub } = useAuth();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (user) navigate('/', { replace: true }); }, [user, navigate]);

  const signIn = async () => {
    setBusy(true);
    setError('');
    try { await signInWithGithub(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'GitHub sign-in could not be started. Please try again.'); }
    finally { setBusy(false); }
  };

  return <main className="auth-page">
    <div className="auth-frame"><section className="auth-story">
      <Link to="/" className="auth-brand"><span className="brand-mark"><Code2 size={17}/><i/></span><span>PR <b>Mentor</b></span></Link>
      <div className="story-content"><div className="story-kicker"><Sparkles size={14}/> THE DEVELOPER'S REVIEW WORKSPACE</div><h1>Your journey to<br/>better code starts here.</h1><p>Review pull requests, understand complex code changes, discover bugs, and become a better developer with AI-powered guidance.</p><div className="story-features"><span><Check size={15}/> AI-powered code reviews</span><span><Check size={15}/> Understand every pull request</span><span><Check size={15}/> Learn through real-world code</span></div><div className="code-art" aria-hidden="true"><div className="code-top"><i/><i/><i/><span>review.ts</span></div><div className="code-lines"><span>01</span><code><b>const</b> insight = <em>review</em>(pullRequest);</code><span>02</span><code><b>if</b> (insight.<i>hasEvidence</i>) {'{'}</code><span>03</span><code>&nbsp;&nbsp;<u>share</u>(insight);</code><span>04</span><code>{'}'}</code></div><div className="code-glow"/></div></div>
      <div className="story-foot"><ShieldCheck size={14}/> Built for thoughtful, evidence-first code review</div>
    </section>
    <section className="auth-panel"><div className="auth-card"><div className="auth-mobile-brand"><span className="brand-mark"><Code2 size={17}/><i/></span> PR <b>Mentor</b></div>
      <div className="auth-heading"><span className="auth-step">WELCOME BACK</span><h2>Welcome to PR Mentor</h2><p>Sign in securely with your GitHub account.</p></div>
      <div className="auth-options"><button type="button" className="auth-provider github-provider" disabled={busy || loading || !configured} onClick={() => void signIn()}><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .9a11.1 11.1 0 0 0-3.51 21.63c.56.1.76-.24.76-.54v-2.1c-3.1.68-3.76-1.32-3.76-1.32-.51-1.3-1.24-1.65-1.24-1.65-1.01-.69.08-.68.08-.68 1.12.08 1.71 1.15 1.71 1.15 1 1.71 2.62 1.22 3.26.93.1-.72.39-1.22.71-1.5-2.47-.28-5.07-1.24-5.07-5.51 0-1.22.44-2.22 1.15-3-.12-.28-.5-1.42.11-2.96 0 0 .94-.3 3.05 1.15a10.6 10.6 0 0 1 5.55 0c2.11-1.45 3.05-1.15 3.05-1.15.61 1.54.23 2.68.11 2.96.72.78 1.15 1.78 1.15 3.01 0 4.28-2.6 5.22-5.08 5.49.4.35.76 1.02.76 2.06v3.12c0 .3.2.65.77.54A11.1 11.1 0 0 0 12 .9Z"/></svg>{busy ? <><LoaderCircle className="spin" size={16}/> Connecting to GitHub…</> : <>Continue with GitHub <ArrowRight size={15}/></>}</button></div>
      {!configured && <div className="auth-note" role="status">{configurationError}</div>}
      {(error || authError) && <div className="auth-error" role="alert">{error || authError}</div>}
      <p className="auth-terms">By continuing, you agree to our <Link to="/terms">Terms of Service</Link> and <Link to="/privacy">Privacy Policy</Link>.</p>
      {loading && <div className="auth-restoring" role="status"><LoaderCircle className="spin" size={14}/> Restoring session…</div>}
    </div><div className="auth-panel-foot"><ShieldCheck size={14}/> Secure authentication powered by Supabase</div></section></div>
  </main>;
}
