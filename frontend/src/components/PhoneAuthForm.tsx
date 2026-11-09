import { useState, type FormEvent } from 'react';
import { ArrowLeft, LoaderCircle, MessageSquareText, Phone } from 'lucide-react';
import { supabase, supabaseConfigurationError } from '../services/supabase';

type PhoneAuthFormProps = {
  disabled?: boolean;
};

type Step = 'phone' | 'code';

function normalizePhone(phone: string) {
  return phone.replace(/[\s()-]/g, '');
}

function isE164(phone: string) {
  return /^\+[1-9]\d{7,14}$/.test(phone);
}

function readableAuthError(reason: unknown) {
  if (reason && typeof reason === 'object') {
    const authError = reason as { code?: string; message?: string };
    const code = authError.code?.toLowerCase() ?? '';
    const message = authError.message?.toLowerCase() ?? '';

    if (code === 'unsupported_phone_provider' || message.includes('unsupported phone provider')) {
      return 'Phone sign-in is not enabled for this Supabase project. In Supabase, open Authentication → Sign In / Providers, enable Phone, configure an SMS provider (such as Twilio or Vonage), and save the settings.';
    }
    if (code === 'phone_provider_disabled' || code === 'provider_disabled') {
      return 'Phone sign-in is disabled for this Supabase project. Enable the Phone provider under Authentication → Sign In / Providers.';
    }
    if (code === 'over_sms_send_rate_limit') {
      return 'Too many codes were requested for this number. Wait a minute before trying again.';
    }
    if (code === 'sms_send_failed' || message.includes('sms provider')) {
      return 'Supabase could not send the SMS. Check that the SMS provider credentials and sender settings are valid in Supabase Authentication.';
    }
    if (authError.message) return authError.message;
  }

  if (reason instanceof Error) return reason.message;
  return 'Phone sign-in could not be completed. Please try again.';
}

export default function PhoneAuthForm({ disabled = false }: PhoneAuthFormProps) {
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [step, setStep] = useState<Step>('phone');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const requestCode = async () => {
    setError('');
    setNotice('');
    if (!supabase) {
      setError(supabaseConfigurationError);
      return;
    }

    const normalizedPhone = normalizePhone(phone);
    if (!isE164(normalizedPhone)) {
      setError('Enter your phone number with its country code, for example +14155552671.');
      return;
    }

    setBusy(true);
    try {
      const { error: sendError } = await supabase.auth.signInWithOtp({ phone: normalizedPhone });
      if (sendError) throw sendError;
      setPhone(normalizedPhone);
      setCode('');
      setStep('code');
      setNotice(`A verification code was sent to ${normalizedPhone}.`);
    } catch (reason) {
      setError(readableAuthError(reason));
    } finally {
      setBusy(false);
    }
  };

  const verifyCode = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setNotice('');
    if (!supabase) {
      setError(supabaseConfigurationError);
      return;
    }
    if (!/^\d{6}$/.test(code.trim())) {
      setError('Enter the six-digit verification code from your text message.');
      return;
    }

    setBusy(true);
    try {
      const { error: verifyError } = await supabase.auth.verifyOtp({
        phone,
        token: code.trim(),
        type: 'sms',
      });
      if (verifyError) throw verifyError;
      setNotice('Phone verified. Finishing sign-in…');
    } catch (reason) {
      setError(readableAuthError(reason));
    } finally {
      setBusy(false);
    }
  };

  const changeNumber = () => {
    setStep('phone');
    setCode('');
    setError('');
    setNotice('');
  };

  return <section className="phone-auth" aria-labelledby="phone-auth-title">
    <div className="auth-divider" aria-hidden="true"><span/>OR<span/></div>
    <div className="phone-auth-heading">
      <h3 id="phone-auth-title"><Phone size={15}/> Continue with phone</h3>
      <p>Sign in or create an account with a one-time SMS code.</p>
    </div>

    {!supabase && <p className="auth-note" role="status">{supabaseConfigurationError}</p>}

    {step === 'phone' ? <form className="phone-form" onSubmit={(event) => { event.preventDefault(); void requestCode(); }}>
      <label htmlFor="phone-number">Phone number</label>
      <input
        id="phone-number"
        type="tel"
        autoComplete="tel"
        inputMode="tel"
        placeholder="+1 415 555 2671"
        value={phone}
        onChange={(event) => setPhone(event.target.value)}
        disabled={disabled || busy || !supabase}
        aria-describedby="phone-number-help"
      />
      <small id="phone-number-help">Include the country code, for example +1 or +91.</small>
      <button className="auth-provider phone-primary" type="submit" disabled={disabled || busy || !supabase}>
        {busy ? <><LoaderCircle className="spin" size={16}/> Sending code…</> : <><MessageSquareText size={16}/> Send verification code</>}
      </button>
    </form> : <form className="phone-form" onSubmit={(event) => void verifyCode(event)}>
      <label htmlFor="phone-code">Verification code</label>
      <input
        id="phone-code"
        className="otp-input"
        type="text"
        autoComplete="one-time-code"
        inputMode="numeric"
        pattern="[0-9]*"
        maxLength={6}
        placeholder="••••••"
        value={code}
        onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
        disabled={busy}
        aria-describedby="phone-code-help"
      />
      <small id="phone-code-help">Enter the six-digit code sent to {phone}.</small>
      <button className="auth-provider phone-primary" type="submit" disabled={busy || code.length !== 6}>
        {busy ? <><LoaderCircle className="spin" size={16}/> Verifying…</> : <>Verify and continue <Phone size={15}/></>}
      </button>
      <div className="otp-actions">
        <button type="button" onClick={changeNumber} disabled={busy}><ArrowLeft size={13}/> Use a different number</button>
        <button type="button" onClick={() => void requestCode()} disabled={busy || !supabase}>Resend code</button>
      </div>
    </form>}

    {error && <div className="auth-error" role="alert">{error}</div>}
    {notice && <div className="auth-note" role="status">{notice}</div>}
  </section>;
}
