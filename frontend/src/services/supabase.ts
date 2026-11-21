import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL?.trim();
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY?.trim();

export const normalizedSupabaseUrl = supabaseUrl
  ?.replace(/\/rest\/v1\/?$/i, '')
  .replace(/\/+$/, '');

export const supabaseConfigurationError = (() => {
  if (!normalizedSupabaseUrl || !supabaseAnonKey) {
    return 'Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in the environment used to build the frontend.';
  }
  if (supabaseAnonKey.startsWith('sb_secret_')) {
    return 'The configured key is a Supabase secret key. Replace it with the project publishable key (sb_publishable_…) or legacy anon key.';
  }
  try {
    const url = new URL(normalizedSupabaseUrl);
    if (url.protocol !== 'https:' && url.hostname !== 'localhost' && url.hostname !== '127.0.0.1') {
      return 'VITE_SUPABASE_URL must use HTTPS outside local development.';
    }
  } catch {
    return 'VITE_SUPABASE_URL must be the Supabase project URL, such as https://your-project-ref.supabase.co.';
  }
  return '';
})();

export const supabaseConfigured = !supabaseConfigurationError;

function withoutProviderCredentials(value: string) {
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === 'object' && ('provider_token' in parsed || 'provider_refresh_token' in parsed)) {
      delete parsed.provider_token;
      delete parsed.provider_refresh_token;
      return JSON.stringify(parsed);
    }
  } catch { /* Supabase also uses storage for values that are not session objects. */ }
  return value;
}

try {
  window.sessionStorage.removeItem('pr-mentor-github-provider-token');
  window.sessionStorage.removeItem('pr-mentor-github-provider-user');
} catch { /* Legacy storage can be unavailable in restricted browsers. */ }

export const supabase = supabaseConfigured
  ? createClient(normalizedSupabaseUrl!, supabaseAnonKey!, {
      auth: {
        autoRefreshToken: true,
        detectSessionInUrl: (url, params) => !url.searchParams.has('prm_github_state')
          && !params.provider_token && Boolean(params.access_token || params.error_description),
        persistSession: true,
        storage: {
          getItem(key) {
            const stored = window.localStorage.getItem(key);
            if (stored === null) return null;
            const sanitized = withoutProviderCredentials(stored);
            if (sanitized !== stored) window.localStorage.setItem(key, sanitized);
            return sanitized;
          },
          setItem(key, value) { window.localStorage.setItem(key, withoutProviderCredentials(value)); },
          removeItem(key) { window.localStorage.removeItem(key); },
        },
      },
    })
  : null;
