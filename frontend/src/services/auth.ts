import { supabase } from './supabase';

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '');

export async function syncSupabaseUser(token: string) {
  const response = await fetch(`${API_BASE}/api/auth/supabase/verify`, {
    method: 'POST', headers: { Authorization: `Bearer ${token}` },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || 'Could not verify your account with PR Mentor.');
  return payload.user;
}

export async function authenticatedFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const getToken = async (refresh = false) => {
    if (!supabase) return undefined;
    if (refresh) {
      const { data } = await supabase.auth.refreshSession();
      return data.session?.access_token;
    }
    const { data } = await supabase.auth.getSession();
    const session = data.session;
    if (!session) return undefined;
    if (session.expires_at && session.expires_at <= Math.floor(Date.now() / 1000) + 30) {
      const { data: refreshed } = await supabase.auth.refreshSession();
      return refreshed.session?.access_token;
    }
    return session.access_token;
  };

  const send = (token?: string) => {
    const headers = new Headers(init.headers);
    if (token) headers.set('Authorization', `Bearer ${token}`);
    else headers.delete('Authorization');
    return fetch(input, { ...init, headers });
  };

  const response = await send(await getToken());
  if (response.status !== 401 || !supabase) return response;

  // Mobile browsers can suspend background tabs past the JWT's expiry. Refresh
  // the persisted Supabase session once, then retry the API call with its new JWT.
  const refreshedToken = await getToken(true);
  return refreshedToken ? send(refreshedToken) : response;
}
