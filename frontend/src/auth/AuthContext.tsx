import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import * as api from "../api/client";
import type { AuthSession, Firm, User, UserRole } from "../types";

interface AuthState {
  user: User | null;
  firm: Firm | null;
  /** True until the initial session restore has settled. */
  loading: boolean;
  signIn: (email: string, password: string, totpCode?: string) => Promise<void>;
  signUp: (payload: api.SignupPayload) => Promise<void>;
  signOut: () => Promise<void>;
  /** True when the current user holds at least `minimum` authority. */
  hasRole: (minimum: UserRole) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

// Ordered by authority; mirrors _ROLE_RANK in the backend's app/deps.py.
const ROLE_RANK: Record<UserRole, number> = {
  CLIENT_VIEWER: 0,
  ASSOCIATE: 1,
  MANAGER: 2,
  PARTNER: 3,
  OWNER: 4,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [firm, setFirm] = useState<Firm | null>(null);
  const [loading, setLoading] = useState(true);

  const apply = useCallback((session: AuthSession) => {
    api.setAccessToken(session.access_token);
    setUser(session.user);
    setFirm(session.firm);
  }, []);

  const clear = useCallback(() => {
    api.setAccessToken(null);
    setUser(null);
    setFirm(null);
  }, []);

  /**
   * On load, try to restore the session from the httpOnly refresh cookie.
   *
   * The access token lives only in memory, so a page reload always starts
   * signed out until this succeeds. A failure here is the normal case for a
   * visitor who has never signed in, so it is not surfaced as an error.
   */
  useEffect(() => {
    let cancelled = false;
    api
      .restoreSession()
      .then((session) => {
        if (!cancelled) apply(session);
      })
      .catch(() => {
        if (!cancelled) clear();
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apply, clear]);

  // When a refresh ultimately fails mid-session, drop back to signed out
  // rather than leaving the UI showing stale data it can no longer load.
  useEffect(() => {
    api.setUnauthorizedHandler(() => clear());
    return () => api.setUnauthorizedHandler(null);
  }, [clear]);

  const signIn = useCallback(
    async (email: string, password: string, totpCode?: string) => {
      apply(await api.login(email, password, totpCode));
    },
    [apply],
  );

  const signUp = useCallback(
    async (payload: api.SignupPayload) => {
      apply(await api.signup(payload));
    },
    [apply],
  );

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      clear();
    }
  }, [clear]);

  const hasRole = useCallback(
    (minimum: UserRole) => {
      if (!user) return false;
      return ROLE_RANK[user.role] >= ROLE_RANK[minimum];
    },
    [user],
  );

  const value = useMemo(
    () => ({ user, firm, loading, signIn, signUp, signOut, hasRole }),
    [user, firm, loading, signIn, signUp, signOut, hasRole],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside an AuthProvider");
  return ctx;
}

export { ROLE_RANK };
