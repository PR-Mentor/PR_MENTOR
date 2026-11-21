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
  sessionStorage.setItem(PROOF_KEY, JSON.stringify({ proof, intent }));
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
    const pending = sessionStorage.getItem(PROOF_KEY);
    sessionStorage.removeItem(PROOF_KEY);
    if (!callback.code || !pending) throw new Error('GitHub authorization was canceled or expired. Start the connection again.');
    const { proof, intent } = JSON.parse(pending) as { proof: string; intent: OAuthIntent };
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
