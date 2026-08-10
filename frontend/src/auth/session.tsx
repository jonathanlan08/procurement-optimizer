/** Auth session state + guards — PRINCIPAL-OWNED. */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import { ApiError, get, post, setCsrfToken } from "../api/client";

export interface SessionInfo {
  user_id: string;
  email: string;
  full_name: string;
  organization_id: string;
  organization_name: string;
  organization_slug: string;
  role: string;
  csrf_token?: string | null;
  demo_mode: boolean;
}

interface AuthState {
  session: SessionInfo | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // a hard refresh keeps the cookie but loses the in-memory CSRF token;
    // /me returns it so the session survives refreshes
    get<SessionInfo>("/api/v1/auth/me")
      .then((s) => {
        setCsrfToken(s.csrf_token ?? null);
        setSession(s);
      })
      .catch(() => setSession(null))
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const s = await post<SessionInfo>("/api/v1/auth/login", { email, password });
    setCsrfToken(s.csrf_token ?? null);
    setSession(s);
  }, []);

  const logout = useCallback(async () => {
    try {
      await post("/api/v1/auth/logout");
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
    } finally {
      setCsrfToken(null);
      setSession(null);
    }
  }, []);

  const value = useMemo(
    () => ({ session, loading, login, logout }),
    [session, loading, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="page-loading" role="status" aria-label="Loading" />;
  if (!session) return <Navigate to="/login" state={{ from: location }} replace />;
  return <>{children}</>;
}
