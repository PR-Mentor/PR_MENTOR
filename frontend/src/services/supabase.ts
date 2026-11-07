import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL?.trim();
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY?.trim();

 codex/fix-blank-auth
export const supabaseConfigured = Boolean(supabaseUrl && supabaseAnonKey);
export const supabase = supabaseConfigured
  ? createClient(supabaseUrl!, supabaseAnonKey!, {

const normalizedSupabaseUrl = supabaseUrl
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
export const supabase = supabaseConfigured
  ? createClient(normalizedSupabaseUrl!, supabaseAnonKey!, {
 main
      auth: {
        autoRefreshToken: true,
        detectSessionInUrl: true,
        persistSession: true,
      },
    })
  : null;
