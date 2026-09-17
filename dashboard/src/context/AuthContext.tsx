import { createContext, useContext } from "react";
import { useCurrentUser } from "@retina/shared";
import { api } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const { user, loading, setUser } = useCurrentUser();

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
