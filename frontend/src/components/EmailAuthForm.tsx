import { useState, type FormEvent } from 'react';
import { ArrowLeft, AtSign, LoaderCircle, MailCheck } from 'lucide-react';
import { supabase, supabaseConfigurationError } from '../services/supabase';

type Step = 'email' | 'code';
type EmailIntent = 'login' | 'signup';
type OtpType = 'email' | 'signup';

export default function EmailAuthForm({ disabled = false }: { disabled?: boolean }) {
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [intent, setIntent] = useState<EmailIntent>('login');
  const [otpType, setOtpType] = useState<OtpType>('email');
  const [step, setStep] = useState<Step>('email');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const requestCode = async () => {
    setError(''); setNotice('');
    if (!supabase) { setError(supabaseConfigurationError); return; }
    const normalizedEmail = email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedEmail)) { setError('Enter a valid email address.'); return; }
    setBusy(true);
    try {
      if (intent === 'signup') {
        // Supabase returns an obfuscated user with no identities when this
        // address already belongs to an account (email enumeration protection).
        const passwordBytes = crypto.getRandomValues(new Uint8Array(32));
        const generatedPassword = Array.from(passwordBytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
        const { data, error: signUpError } = await supabase.auth.signUp({ email: normalizedEmail, password: generatedPassword });
        if (signUpError) throw signUpError;
        if (data.user && Array.isArray(data.user.identities) && data.user.identities.length === 0) {
          setError('An account with this email already exists. Please log in instead.');
          return;
        }
        setOtpType('signup');
      } else {
        const { error: sendError } = await supabase.auth.signInWithOtp({ email: normalizedEmail, options: { shouldCreateUser: false } });
        if (sendError) throw sendError;
        setOtpType('email');
      }
      setEmail(normalizedEmail); setCode(''); setStep('code'); setNotice(`A verification code was sent to ${normalizedEmail}.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Email code could not be sent. Please try again.');
    } finally { setBusy(false); }
  };

  const verifyCode = async (event: FormEvent) => {
    event.preventDefault(); setError(''); setNotice('');
    if (!supabase) { setError(supabaseConfigurationError); return; }
    if (!/^\d{6}$/.test(code.trim())) { setError('Enter the six-digit verification code from your email.'); return; }
    setBusy(true);
    try {
      const { error: verifyError } = await supabase.auth.verifyOtp({ email, token: code.trim(), type: otpType });
      if (verifyError) throw verifyError;
      setNotice('Email verified. Finishing sign-in…');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Email verification failed. Please try again.'); }
    finally { setBusy(false); }
  };

  const changeEmail = () => { setStep('email'); setCode(''); setError(''); setNotice(''); };

  return <section className="phone-auth email-auth" aria-labelledby="email-auth-title">
    <div className="auth-divider" aria-hidden="true"><span/>OR<span/></div>
    <div className="phone-auth-heading"><h3 id="email-auth-title"><AtSign size={15}/> Continue with email</h3><p>Use a one-time email code to access PR Mentor.</p></div>
    {!supabase && <p className="auth-note" role="status">{supabaseConfigurationError}</p>}
    {step === 'email' ? <form className="phone-form" onSubmit={(event) => { event.preventDefault(); void requestCode(); }}>
      <div className="email-auth-modes" role="group" aria-label="Choose email authentication">
        <button type="button" className={intent === 'login' ? 'active' : ''} aria-pressed={intent === 'login'} onClick={() => setIntent('login')} disabled={busy}>Log in</button>
        <button type="button" className={intent === 'signup' ? 'active' : ''} aria-pressed={intent === 'signup'} onClick={() => setIntent('signup')} disabled={busy}>Create account</button>
      </div>
      <p className="email-auth-hint">{intent === 'login' ? 'Already have an account? We’ll email you a secure sign-in code.' : 'New to PR Mentor? We’ll create your account after you verify your email.'}</p>
      <label htmlFor="email-address">Email address</label>
      <input id="email-address" type="email" autoComplete="email" placeholder="you@example.com" value={email} onChange={(event) => setEmail(event.target.value)} disabled={disabled || busy || !supabase} required />
      <button className="auth-provider phone-primary" type="submit" disabled={disabled || busy || !supabase}>{busy ? <><LoaderCircle className="spin" size={16}/> Sending code…</> : <><MailCheck size={16}/> {intent === 'login' ? 'Send login code' : 'Send sign-up code'}</>}</button>
    </form> : <form className="phone-form" onSubmit={(event) => void verifyCode(event)}>
      <label htmlFor="email-code">Verification code</label>
      <input id="email-code" className="otp-input" type="text" autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]*" maxLength={6} placeholder="••••••" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} disabled={busy} aria-describedby="email-code-help" />
      <small id="email-code-help">Enter the six-digit code sent to {email}.</small>
      <button className="auth-provider phone-primary" type="submit" disabled={busy || code.length !== 6}>{busy ? <><LoaderCircle className="spin" size={16}/> Verifying…</> : <>Verify and continue <MailCheck size={15}/></>}</button>
      <div className="otp-actions"><button type="button" onClick={changeEmail} disabled={busy}><ArrowLeft size={13}/> Use a different email</button><button type="button" onClick={() => void requestCode()} disabled={busy || !supabase}>Resend code</button></div>
    </form>}
    {error && <div className="auth-error" role="alert">{error}</div>}{notice && <div className="auth-note" role="status">{notice}</div>}
  </section>;
}
