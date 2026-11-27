import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, refreshSession, tokenStore, UNAUTHORIZED_EVENT } from "../api/client";
import type { User } from "../api/types";

interface AuthState {
  user: User | null;
  loading: boolean; // true while the silent login (refresh cookie) is checked
  login: (email: string, password: string) => Promise<void>;
  register: (name: string, email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // On start the access token (memory only) is gone: the refresh cookie, if the
  // user has a valid session, buys a new one ("silent login").
  useEffect(() => {
    refreshSession()
      .then((ok) => (ok ? api.me().then(setUser) : undefined))
      .catch(() => tokenStore.clear())
      .finally(() => setLoading(false));
  }, []);

  // Any 401 (expired token, deleted user) -> logged out everywhere.
  useEffect(() => {
    const onUnauthorized = () => setUser(null);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const token = await api.login(email, password);
    tokenStore.set(token.access_token);
    setUser(await api.me());
  }, []);

  const register = useCallback(async (name: string, email: string, password: string) => {
    await api.register(name, email, password);
    await login(email, password);
  }, [login]);

  const logout = useCallback(() => {
    // Revoke the session on the server (and clear the cookie). The UI logs out
    // immediately either way - a network error must not keep the user logged in.
    void api.logout().catch(() => {});
    tokenStore.clear();
    setUser(null);
  }, []);

  const value = useMemo(() => ({ user, loading, login, register, logout }), [user, loading, login, register, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
