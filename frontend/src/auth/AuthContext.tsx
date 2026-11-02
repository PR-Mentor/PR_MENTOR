import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { onAuthStateChanged, signOut } from 'firebase/auth';
import type { User as FirebaseUser } from 'firebase/auth';
import { firebaseAuth } from '../services/firebase';
import { syncFirebaseUser } from '../services/auth';

export type UserProfile = {
  firebase_uid: string;
  username: string;
  display_name: string;
  email: string | null;
  avatar_url: string | null;
  providers: string[];
  preferences: { theme: string; explanation_level: string; notifications: boolean };
};

type AuthValue = { user: UserProfile | null; firebaseUser: FirebaseUser | null; loading: boolean; error: string; logout: () => Promise<void> };
const Context = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [firebaseUser, setFirebaseUser] = useState<FirebaseUser | null>(null);
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!firebaseAuth) { setLoading(false); return; }
    return onAuthStateChanged(firebaseAuth, async (next) => {
      setFirebaseUser(next);
      setError('');
      if (!next) { setUser(null); setLoading(false); return; }
      setLoading(true);
      try { setUser(await syncFirebaseUser(next)); }
      catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not load your account.'); setUser(null); }
      finally { setLoading(false); }
    });
  }, []);

  const value = useMemo<AuthValue>(() => ({
    user, firebaseUser, loading, error,
    logout: async () => { if (firebaseAuth) await signOut(firebaseAuth); setUser(null); },
  }), [user, firebaseUser, loading, error]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useAuth() {
  const value = useContext(Context);
  if (!value) throw new Error('useAuth must be used inside AuthProvider.');
  return value;
}
