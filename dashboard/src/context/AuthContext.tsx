import { createContext, useContext, useState, useEffect } from "react";
import { api, UnauthorizedError } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function fetchUser() {
      for (let attempt = 0; attempt < 4; attempt++) {
        try {
          const u = await api.me();
          if (!cancelled) { setUser(u); setLoading(false); }
          return;
        } catch (e) {
          // A 401 is settled, so stop: the retries exist for a busy server, and
          // repeating an unauthenticated call only holds the login card behind
          // a loading state for the length of the backoff.
          if (e instanceof UnauthorizedError) break;
          if (attempt < 3) {
            await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
          }
        }
      }
      if (!cancelled) { setUser(null); setLoading(false); }
    }

    fetchUser();
    return () => { cancelled = true; };
  }, []);

  // Resolves { redirected } so a caller knows not to route over a navigation
  // that is still in flight.
  const logout = async () => {
    const result = await api.logout();
    const target = result?.redirect;
    // Only a top-level navigation reaches the edge, and only the edge can end
    // an Access session. Same-origin paths only, as _safe_redirect enforces
    // server-side. Left bare deliberately: the server sends /cdn-cgi/access/
    // logout, an edge path that lives at the origin root, so prefixing it with
    // this bundle's mount would send it to a route that does not exist.
    if (typeof target === "string" && target.startsWith("/") && !target.startsWith("//")) {
      // No setUser: RequireAuth bounces to /login the moment it goes falsy, and
      // the page is leaving anyway.
      window.location.assign(target);
      return { redirected: true };
    }
    setUser(null);
    return { redirected: false };
  };

  return (
    <AuthContext.Provider value={{ user, loading, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
