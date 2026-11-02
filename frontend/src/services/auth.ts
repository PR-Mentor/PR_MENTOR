import type { User as FirebaseUser } from 'firebase/auth';
import { firebaseAuth } from './firebase';

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '');

export async function syncFirebaseUser(firebaseUser: FirebaseUser) {
  const token = await firebaseUser.getIdToken();
  const response = await fetch(`${API_BASE}/api/auth/firebase/verify`, {
    method: 'POST', headers: { Authorization: `Bearer ${token}` },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || 'Could not verify your account with PR Mentor.');
  return payload.user;
}

export async function authenticatedFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const token = await firebaseAuth?.currentUser?.getIdToken();
  return fetch(input, { ...init, headers: { ...init.headers, ...(token ? { Authorization: `Bearer ${token}` } : {}) } });
}
