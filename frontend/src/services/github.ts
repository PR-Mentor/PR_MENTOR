import { request } from './api';
import { normalizedSupabaseUrl, supabase } from './supabase';

export type GitHubProfile = {
  id: number;
  login: string;
  avatar_url: string | null;
  html_url: string;
  email: string | null;
};

export type GitHubConnection = { connected: boolean; profile: GitHubProfile | null };

export type GitHubRepository = {
  id: number;
  name: string;
  full_name: string;
  description: string | null;
  html_url: string;
  owner: { login: string; avatar_url: string | null };
  visibility: string;
  language: string | null;
  stargazers_count: number;
  forks_count: number;
  default_branch: string;
  updated_at: string;
  open_issues_count: number;
};

export type GitHubPullRequest = {
  number: number;
  title: string;
  body: string | null;
  author: string | null;
  head: string;
  base: string;
  created_at: string;
  updated_at: string;
  state: string;
  draft: boolean;
  html_url: string;
  changed_files: number | null;
  additions: number | null;
  deletions: number | null;
};

type Page<T> = { items: T[]; has_next: boolean };
type OAuthIntent = 'login' | 'connect';

const GITHUB_API = '/api/github';
const PROOF_KEY = 'pr-mentor-github-oauth-proof';
const STATE_PARAM = 'prm_github_state';
const PROOF_MAX_AGE_MS = 10 * 60 * 1000;

type PendingOAuth = { proof: string; intent: OAuthIntent; createdAt: number };

function pendingProofKey(state: string) {
  return `${PROOF_KEY}:${state}`;
}

function savePendingProof(state: string, proof: string, intent: OAuthIntent) {
  const serialized = JSON.stringify({ proof, intent, createdAt: Date.now() } satisfies PendingOAuth);
  const key = pendingProofKey(state);
  let saved = false;

  try {
    localStorage.setItem(key, serialized);
    saved = true;
  } catch {
    // Some browsers restrict persistent storage. Try tab storage as a fallback.
  }

  try {
    sessionStorage.setItem(key, serialized);
    saved = true;
  } catch {
    // Report a useful error below if both browser storage options are blocked.
  }

  if (!saved) throw new Error('Your browser blocked sign-in storage. Allow site storage and try again.');
}

function takePendingProof(state: string): { proof: string; intent: OAuthIntent } | null {
  const key = pendingProofKey(state);
  let serialized: string | null = null;

  try {
    serialized = localStorage.getItem(key);
    localStorage.removeItem(key);
  } catch {
    // Continue with tab storage for browsers that block local storage.
  }

  try {
    serialized ??= sessionStorage.getItem(key);
    sessionStorage.removeItem(key);
    // Consume credentials created by the previous, tab-only flow during rollout.
    if (!serialized) {
      serialized = sessionStorage.getItem(PROOF_KEY);
      sessionStorage.removeItem(PROOF_KEY);
    }
  } catch {
    return null;
  }

  if (!serialized) return null;

  try {
    const pending = JSON.parse(serialized) as Partial<PendingOAuth>;
    const createdAt = pending.createdAt === undefined ? Date.now() : Number(pending.createdAt);
    const age = Date.now() - createdAt;
    if (
      typeof pending.proof !== 'string'
      || !/^[A-Za-z0-9_-]{43}$/.test(pending.proof)
      || (pending.intent !== 'login' && pending.intent !== 'connect')
      || !Number.isFinite(age)
      || age < 0
      || age > PROOF_MAX_AGE_MS
    ) return null;
    return { proof: pending.proof, intent: pending.intent };
  } catch {
    return null;
  }
}

function base64url(bytes: Uint8Array) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export async function startGitHubOAuth(intent: OAuthIntent) {
  const returnUrl = new URL(import.meta.env.BASE_URL, window.location.origin);
  const proof = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(proof));
  const { authorization_url } = await request<{ authorization_url: string }>(`${GITHUB_API}/oauth/start`, {
    method: 'POST', body: JSON.stringify({ challenge: base64url(new Uint8Array(digest)), intent, return_url: returnUrl.href }),
  });
  const authorization = new URL(authorization_url);
  const expected = normalizedSupabaseUrl && new URL(`${normalizedSupabaseUrl}/auth/v1/authorize`);
  if (!expected || authorization.origin !== expected.origin || authorization.pathname !== expected.pathname || authorization.username || authorization.password) {
    throw new Error('The frontend and API must use the same Supabase project for GitHub sign-in.');
  }
  const redirectTo = authorization.searchParams.get('redirect_to');
  const state = redirectTo && new URL(redirectTo).searchParams.get(STATE_PARAM);
  if (!state) throw new Error('GitHub sign-in could not be started. Please try again.');
  savePendingProof(state, proof, intent);
  window.location.assign(authorization.href);
}

function readCallback() {
  const url = new URL(window.location.href);
  const state = url.searchParams.get(STATE_PARAM);
  const fragment = new URLSearchParams(url.hash.slice(1));
  if (fragment.has('provider_token')) {
    // Clear redirects left by frontend versions that used implicit OAuth.
    url.hash = '';
    window.history.replaceState(window.history.state, '', url);
  }
  if (!state) return null;
  const code = url.searchParams.get('code');
  const target = new URL('repositories', new URL(import.meta.env.BASE_URL, window.location.origin));
  window.history.replaceState(window.history.state, '', target);
  return { state, code };
}

const callback = readCallback();
let callbackPromise: Promise<void> | undefined;

export const hasGitHubCallback = Boolean(callback);

export function finishGitHubOAuth() {
  callbackPromise ??= (async () => {
    if (!callback) return;
    if (!callback.code) throw new Error('GitHub authorization was canceled or expired. Start the connection again.');
    const pending = takePendingProof(callback.state);
    if (!pending) throw new Error('The sign-in session could not be restored in this browser. Start GitHub sign-in again.');
    const { proof, intent } = pending;
    const { session } = await request<{ session: { access_token: string; refresh_token: string } | null }>(`${GITHUB_API}/oauth/callback`, {
      method: 'POST', body: JSON.stringify({ ...callback, proof, intent }),
    });
    if (session && supabase) {
      const { error } = await supabase.auth.setSession(session);
      if (error) throw error;
    }
  })();
  return callbackPromise;
}

export function getGitHubConnection(signal?: AbortSignal) {
  return request<GitHubConnection>(`${GITHUB_API}/connection`, { signal });
}

export function disconnectGitHub() {
  return request<{ connected: false; revoked: boolean | null }>(`${GITHUB_API}/connection`, { method: 'DELETE' });
}

export function getRepositories(refresh = false, signal?: AbortSignal) {
  return request<GitHubRepository[]>(`${GITHUB_API}/repositories${refresh ? '/refresh' : ''}`, { method: refresh ? 'POST' : 'GET', signal });
}

function repositoryUrl(owner: string, repo: string) {
  return `${GITHUB_API}/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
}

export function getRepository(owner: string, repo: string, signal?: AbortSignal) {
  return request<GitHubRepository>(repositoryUrl(owner, repo), { signal });
}

export function getBranches(owner: string, repo: string, page: number, signal?: AbortSignal) {
  return request<Page<{ name: string; protected: boolean }>>(`${repositoryUrl(owner, repo)}/branches?page=${page}`, { signal });
}

export function getPullRequests(owner: string, repo: string, page: number, signal?: AbortSignal) {
  return request<Page<GitHubPullRequest>>(`${repositoryUrl(owner, repo)}/pulls?page=${page}`, { signal });
}

export function getPullRequest(owner: string, repo: string, number: number, signal?: AbortSignal) {
  return request<GitHubPullRequest>(`${repositoryUrl(owner, repo)}/pulls/${number}`, { signal });
}
