import { createContext, useContext, useEffect, useState } from "react";
import { request, useCurrentUser } from "@retina/shared";
import { api } from "../api/client";

const AuthContext = createContext(null);

/** Whether this server runs a synthetic fleet, for a caller who has no session
 *  to read it off.
 *
 *  A signed-in user gets the same fact as `synthetic_fleet` on /api/auth/me.
 *  A visitor does not, and the console has to decide whether to point at
 *  /sim and mount the physics page under it: test and staging run a fleet,
 *  production does not, and one bundle serves all three. /api/health is the
 *  only thing the server tells everyone, so the flag is answered there, and
 *  AuthProvider folds the two sources into the one `syntheticFleet` it hands
 *  out.
 *
 *  Asked once at boot, beside the /me fetch, and never again — it is a
 *  property of the deployment, which does not change under a running tab. A
 *  failure is swallowed and leaves it false: this decides whether one nav
 *  entry is drawn, and a console that will not render because a liveness probe
 *  timed out would be a far worse trade. */
function useSyntheticFleet(): boolean {
  const [syntheticFleet, setSyntheticFleet] = useState(false);

  useEffect(() => {
    let cancelled = false;
    request<{ synthetic_fleet?: boolean }>("/api/health")
      .then((h) => {
        if (!cancelled) setSyntheticFleet(Boolean(h?.synthetic_fleet));
      })
      .catch(() => {
        /* unadvertised is the safe answer; see above */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return syntheticFleet;
}

export function AuthProvider({ children }) {
  const { user, loading, setUser } = useCurrentUser();
  const healthFleet = useSyntheticFleet();
  // /me's word first: it arrives under the loading gate, so a signed-in
  // caller's pages mount on the first settled render rather than a health
  // round trip later.
  const syntheticFleet = Boolean(user?.synthetic_fleet ?? healthFleet);

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
