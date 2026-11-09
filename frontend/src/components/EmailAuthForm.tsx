import { useState, type FormEvent } from 'react';
import { ArrowLeft, AtSign, LoaderCircle, MailCheck } from 'lucide-react';
import { supabase, supabaseConfigurationError } from '../services/supabase';

type Step = 'email' | 'code';

export default function EmailAuthForm({ disabled = false }: { disabled?: boolean }) {
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
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
      const { error: sendError } = await supabase.auth.signInWithOtp({ email: normalizedEmail, options: { shouldCreateUser: true } });
      if (sendError) throw sendError;
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
      const { error: verifyError } = await supabase.auth.verifyOtp({ email, token: code.trim(), type: 'email' });
      if (verifyError) throw verifyError;
      setNotice('Email verified. Finishing sign-in…');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Email verification failed. Please try again.'); }
    finally { setBusy(false); }
  };

  const changeEmail = () => { setStep('email'); setCode(''); setError(''); setNotice(''); };

  return <section className="phone-auth email-auth" aria-labelledby="email-auth-title">
    <div className="auth-divider" aria-hidden="true"><span/>OR<span/></div>
    <div className="phone-auth-heading"><h3 id="email-auth-title"><AtSign size={15}/> Continue with email</h3><p>Sign in or create an account with a one-time email code.</p></div>
    {!supabase && <p className="auth-note" role="status">{supabaseConfigurationError}</p>}
    {step === 'email' ? <form className="phone-form" onSubmit={(event) => { event.preventDefault(); void requestCode(); }}>
      <label htmlFor="email-address">Email address</label>
      <input id="email-address" type="email" autoComplete="email" placeholder="you@example.com" value={email} onChange={(event) => setEmail(event.target.value)} disabled={disabled || busy || !supabase} required />
      <button className="auth-provider phone-primary" type="submit" disabled={disabled || busy || !supabase}>{busy ? <><LoaderCircle className="spin" size={16}/> Sending code…</> : <><MailCheck size={16}/> Send email code</>}</button>
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
