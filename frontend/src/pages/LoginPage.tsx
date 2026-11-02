import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Code2, ArrowLeft, ArrowRight, Check, LoaderCircle, ShieldCheck, Sparkles, Phone } from 'lucide-react';
import { GithubAuthProvider, GoogleAuthProvider, RecaptchaVerifier, signInWithPhoneNumber, signInWithPopup } from 'firebase/auth';
import type { ConfirmationResult } from 'firebase/auth';
import { useAuth } from '../auth/AuthContext';
import { firebaseAuth, firebaseConfigured } from '../services/firebase';

const countries = [{ code: '+91', name: 'India' }, { code: '+1', name: 'United States' }, { code: '+44', name: 'United Kingdom' }, { code: '+61', name: 'Australia' }, { code: '+49', name: 'Germany' }, { code: '+81', name: 'Japan' }];

export default function LoginPage() {
  const { user, loading, error: profileError } = useAuth();
  const navigate = useNavigate();
  const [phoneMode, setPhoneMode] = useState(false);
  const [confirmation, setConfirmation] = useState<ConfirmationResult | null>(null);
  const [country, setCountry] = useState('+91');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [seconds, setSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const verifier = useRef<RecaptchaVerifier | null>(null);

  useEffect(() => { if (user) navigate('/', { replace: true }); }, [user, navigate]);
  useEffect(() => {
    if (seconds <= 0) return;
    const timer = window.setTimeout(() => setSeconds((value) => value - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [seconds]);
  useEffect(() => () => { verifier.current?.clear(); }, []);

  const providerSignIn = async (provider: 'github' | 'google') => {
    if (!firebaseAuth || !firebaseConfigured) { setError('Authentication is not configured yet. Add Firebase settings to frontend/.env.local.'); return; }
    setBusy(true); setError('');
    try {
      const authProvider = provider === 'github' ? new GithubAuthProvider() : new GoogleAuthProvider();
      if (provider === 'google') authProvider.setCustomParameters({ prompt: 'select_account' });
      if (provider === 'github') authProvider.addScope('read:user');
      await signInWithPopup(firebaseAuth, authProvider);
    } catch (reason) {
      const code = (reason as { code?: string }).code;
      if (code !== 'auth/popup-closed-by-user' && code !== 'auth/cancelled-popup-request') setError(code === 'auth/account-exists-with-different-credential' ? 'This email is already linked to another sign-in method. Sign in with that provider first.' : reason instanceof Error ? reason.message : 'Sign-in failed. Please try again.');
    } finally { setBusy(false); }
  };

  const sendCode = async () => {
    if (!firebaseAuth || !firebaseConfigured) { setError('Authentication is not configured yet. Add Firebase settings to frontend/.env.local.'); return; }
    const digits = phone.replace(/\D/g, '');
    const localLengths: Record<string, number[]> = { '+91': [10], '+1': [10], '+44': [10, 11], '+61': [9], '+49': [7, 8, 9, 10, 11, 12], '+81': [10, 11] };
    if (!localLengths[country]?.includes(digits.length) || (country === '+91' && !/^[6-9]\d{9}$/.test(digits)) || !/^\+[1-9]\d{7,14}$/.test(`${country}${digits}`)) { setError('Enter a valid phone number for the selected country.'); return; }
    setBusy(true); setError('');
    try {
      verifier.current?.clear();
      verifier.current = new RecaptchaVerifier(firebaseAuth, 'phone-recaptcha', { size: 'invisible' });
      const result = await signInWithPhoneNumber(firebaseAuth, `${country}${digits}`, verifier.current);
      setConfirmation(result); setSeconds(30);
    } catch (reason) { verifier.current?.clear(); verifier.current = null; setError(reason instanceof Error ? reason.message : 'Could not send the verification code.'); }
    finally { setBusy(false); }
  };

  const verifyCode = async () => {
    if (!confirmation || !/^\d{6}$/.test(code)) { setError('Enter the six-digit code sent to your phone.'); return; }
    setBusy(true); setError('');
    try { await confirmation.confirm(code); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'That code is invalid or expired.'); }
    finally { setBusy(false); }
  };

  return <main className="auth-page">
    <section className="auth-story">
      <Link to="/" className="auth-brand"><span className="brand-mark"><Code2 size={17}/><i/></span><span>PR <b>Mentor</b></span></Link>
      <div className="story-content"><div className="story-kicker"><Sparkles size={14}/> THE DEVELOPER'S REVIEW WORKSPACE</div><h1>Your journey to<br/>better code starts here.</h1><p>Review pull requests, understand complex code changes, discover bugs, and become a better developer with AI-powered guidance.</p><div className="story-features"><span><Check size={15}/> AI-powered code reviews</span><span><Check size={15}/> Understand every pull request</span><span><Check size={15}/> Learn through real-world code</span></div><div className="code-art" aria-hidden="true"><div className="code-top"><i/><i/><i/><span>review.ts</span></div><div className="code-lines"><span>01</span><code><b>const</b> insight = <em>review</em>(pullRequest);</code><span>02</span><code><b>if</b> (insight.<i>hasEvidence</i>) {'{'}</code><span>03</span><code>&nbsp;&nbsp;<u>share</u>(insight);</code><span>04</span><code>{'}'}</code></div><div className="code-glow"/></div></div>
      <div className="story-foot"><ShieldCheck size={14}/> Built for thoughtful, evidence-first code review</div>
    </section>
    <section className="auth-panel"><div className="auth-card"><div className="auth-mobile-brand"><span className="brand-mark"><Code2 size={17}/><i/></span> PR <b>Mentor</b></div>
      {phoneMode ? <>
        <button className="auth-back" type="button" onClick={() => { setPhoneMode(false); setConfirmation(null); setCode(''); setError(''); }}><ArrowLeft size={15}/> All sign-in options</button>
        <div className="auth-heading"><span className="auth-step">{confirmation ? '02 / VERIFY NUMBER' : '01 / PHONE SIGN IN'}</span><h2>{confirmation ? 'Check your messages' : 'Continue with phone'}</h2><p>{confirmation ? `Enter the six-digit code sent to ${country} ${phone}.` : 'We’ll text you a one-time verification code.'}</p></div>
        {!confirmation ? <div className="phone-form"><label htmlFor="country-code">Country</label><select id="country-code" value={country} onChange={(event) => setCountry(event.target.value)}>{countries.map((item) => <option value={item.code} key={item.code}>{item.name} ({item.code})</option>)}</select><label htmlFor="phone-number">Phone number</label><div className="phone-input"><span>{country}</span><input id="phone-number" autoComplete="tel-national" inputMode="tel" value={phone} onChange={(event) => setPhone(event.target.value)} placeholder="98765 43210"/></div><button className="auth-primary" type="button" disabled={busy} onClick={sendCode}>{busy ? <LoaderCircle className="spin" size={16}/> : null}Send verification code <ArrowRight size={15}/></button><div id="phone-recaptcha"/></div> : <div className="phone-form"><label htmlFor="otp-code">Verification code</label><input className="otp-input" id="otp-code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} placeholder="• • • • • •"/><button className="auth-primary" type="button" disabled={busy} onClick={verifyCode}>{busy ? <LoaderCircle className="spin" size={16}/> : null}Verify and continue <ArrowRight size={15}/></button><div className="otp-actions"><button type="button" onClick={() => { setConfirmation(null); setCode(''); setError(''); }}>Change number</button><button type="button" disabled={seconds > 0 || busy} onClick={sendCode}>{seconds > 0 ? `Resend in ${seconds}s` : 'Resend code'}</button></div><div id="phone-recaptcha"/></div>}
      </> : <>
        <div className="auth-heading"><span className="auth-step">WELCOME BACK</span><h2>Welcome to PR Mentor</h2><p>Sign in to continue your developer journey.</p></div>
        <div className="auth-options"><button type="button" className="auth-provider github-provider" disabled={busy} onClick={() => void providerSignIn('github')}><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .9a11.1 11.1 0 0 0-3.51 21.63c.56.1.76-.24.76-.54v-2.1c-3.1.68-3.76-1.32-3.76-1.32-.51-1.3-1.24-1.65-1.24-1.65-1.01-.69.08-.68.08-.68 1.12.08 1.71 1.15 1.71 1.15 1 1.71 2.62 1.22 3.26.93.1-.72.39-1.22.71-1.5-2.47-.28-5.07-1.24-5.07-5.51 0-1.22.44-2.22 1.15-3-.12-.28-.5-1.42.11-2.96 0 0 .94-.3 3.05 1.15a10.6 10.6 0 0 1 5.55 0c2.11-1.45 3.05-1.15 3.05-1.15.61 1.54.23 2.68.11 2.96.72.78 1.15 1.78 1.15 3.01 0 4.28-2.6 5.22-5.08 5.49.4.35.76 1.02.76 2.06v3.12c0 .3.2.65.77.54A11.1 11.1 0 0 0 12 .9Z"/></svg> Continue with GitHub</button><button type="button" className="auth-provider" disabled={busy} onClick={() => void providerSignIn('google')}><span className="google-g">G</span> Continue with Google</button><div className="auth-divider"><span/>OR<span/></div><button type="button" className="auth-provider" disabled={busy} onClick={() => { setPhoneMode(true); setError(''); }}><Phone size={17}/> Continue with phone number</button></div>
      </>}
      {(error || profileError) && <div className="auth-error" role="alert">{error || profileError}</div>}
      {!firebaseConfigured && <div className="auth-note">Firebase credentials are required before sign-in can be used.</div>}
      <p className="auth-terms">By continuing, you agree to our <a href="/terms">Terms of Service</a> and <a href="/privacy">Privacy Policy</a>.</p>
      {loading && <div className="auth-restoring" role="status"><LoaderCircle className="spin" size={14}/> Restoring session…</div>}
    </div><div className="auth-panel-foot"><ShieldCheck size={14}/> Secure authentication powered by Firebase</div></section>
  </main>;
}
