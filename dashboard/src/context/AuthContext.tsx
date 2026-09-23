import { createContext, useContext } from "react";
import { useCurrentUser } from "@retina/shared";
import { api } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const { user, loading, setUser } = useCurrentUser();
  // Whether this server runs a synthetic fleet, as /api/auth/me tells a
  // session. A visitor reads false, which costs nothing deployed: the
  // simulator is on the admin console, which has no visitors, and every
  // deployed /map is real-only, where ground truth is never offered.
  const syntheticFleet = Boolean(user?.synthetic_fleet);

  // Resolves { redirected } so a caller knows not to route over a navigation
  // that is still in flight. `leave` runs in the same tick the identity is
  // dropped, so a route change it makes renders together with the signed-out
  // state and RequireAuth never judges the old page without a session.
  const logout = async (leave?: () => void) => {
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
    leave?.();
    setUser(null);
    return { redirected: false };
  };

  // signIn adopts an identity the caller already holds. The magic-link
  // redemption answers with the user it just signed in, so there is nothing to
  // fetch; without it the guard would bounce a fresh session to the login card
  // until /api/auth/me was asked again. Passed bare so it stays referentially
  // stable, which callers may depend on in an effect.
  return (
    <AuthContext.Provider value={{ user, loading, logout, signIn: setUser, syntheticFleet }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
