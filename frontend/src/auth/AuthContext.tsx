import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import type { Session, User } from '@supabase/supabase-js';
import { supabase, supabaseConfigured, supabaseConfigurationError } from '../services/supabase';
import { syncSupabaseUser } from '../services/auth';
import { finishGitHubOAuth, hasGitHubCallback, startGitHubOAuth } from '../services/github';

export type UserProfile = {
  firebase_uid: string;
  username: string;
  display_name: string;
  email: string | null;
  avatar_url: string | null;
  providers: string[];
  preferences: { theme: string; explanation_level: string; notifications: boolean };
};

type AuthValue = {
  user: UserProfile | null;
  loading: boolean;
  error: string;
  configured: boolean;
  configurationError: string;
  signInWithGithub: () => Promise<void>;
  logout: () => Promise<void>;
};

const Context = createContext<AuthValue | null>(null);
const DEFAULT_PREFERENCES = { theme: 'dark', explanation_level: 'beginner', notifications: true };

function profileFromSupabaseUser(user: User): UserProfile {
  const metadata = user.user_metadata ?? {};
  const username = String(metadata.user_name ?? metadata.preferred_username ?? user.email?.split('@')[0] ?? user.id.slice(0, 16));
  return {
    firebase_uid: `supabase:${user.id}`,
    username: username.slice(0, 39),
    display_name: String(metadata.full_name ?? metadata.name ?? username).slice(0, 100),
    email: user.email ?? null,
    avatar_url: typeof metadata.avatar_url === 'string' ? metadata.avatar_url : null,
    providers: Array.isArray(user.app_metadata.providers) ? user.app_metadata.providers : [],
    preferences: DEFAULT_PREFERENCES,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [finishingGithub, setFinishingGithub] = useState(hasGitHubCallback);
  const [githubError, setGithubError] = useState('');

  useEffect(() => {
    if (!supabase) {
      setLoading(false);
      return;
    }

    let active = true;
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      if (!active) return;
      setSession(nextSession);
      setUser(nextSession?.user ? profileFromSupabaseUser(nextSession.user) : null);
      setError('');
      setLoading(false);
    });

    void supabase.auth.getSession().then(({ data, error: sessionError }) => {
      if (!active) return;
      if (sessionError) setError(sessionError.message);
      setSession(data.session);
      setUser(data.session?.user ? profileFromSupabaseUser(data.session.user) : null);
      setLoading(false);
    });

    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!hasGitHubCallback) return;
    let active = true;
    void finishGitHubOAuth()
      .catch((reason: unknown) => {
        if (active) setGithubError(reason instanceof Error ? reason.message : 'GitHub authorization could not be completed.');
      })
      .finally(() => { if (active) setFinishingGithub(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!session?.access_token) return;
    let active = true;
    void syncSupabaseUser(session.access_token)
      .then((profile) => { if (active) setUser(profile); })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : 'Could not sync your account with PR Mentor.');
      });
    return () => { active = false; };
  }, [session?.access_token]);

  const signInWithGithub = useCallback(async () => {
    if (!supabase) throw new Error('Add the Supabase project URL and anon key to the frontend environment before signing in.');
    setGithubError('');
    await startGitHubOAuth('login');
  }, []);

  const logout = useCallback(async () => {
    if (!supabase) return;
    const { error: signOutError } = await supabase.auth.signOut();
    if (signOutError) throw signOutError;
    setSession(null);
    setUser(null);
    setGithubError('');
  }, []);

  const value = useMemo<AuthValue>(() => ({
    user, loading: loading || finishingGithub, error: githubError || error,
    configured: supabaseConfigured, configurationError: supabaseConfigurationError, signInWithGithub, logout,
  }), [user, loading, finishingGithub, githubError, error, signInWithGithub, logout]);

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useAuth() {
  const value = useContext(Context);
  if (!value) throw new Error('useAuth must be used inside AuthProvider.');
  return value;
}
