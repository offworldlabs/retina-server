import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { UnauthorizedError, useCurrentUser, type CurrentUser } from "@retina/shared";
import { api } from "../api/client";
import SignOutNotice from "../components/SignOutNotice";
import { describeFailure, type RequestFailure } from "../utils/requestFailure";

/** How a sign-out ended. `failure` is set when the server did not confirm it,
 *  in which case this device is signed out all the same, unless it adopted
 *  another session while the request was out. */
export interface SignOutResult {
  redirected: boolean;
  failure: RequestFailure | null;
}

export interface AuthContextValue {
  user: CurrentUser | null;
  /** True until the identity is settled, one way or the other. */
  loading: boolean;
  logout: (leave?: () => void) => Promise<SignOutResult>;
  signIn: (user: CurrentUser) => void;
  syntheticFleet: boolean;
  polledRadarRegistration: boolean;
  /** A sign-out request is in flight. */
  signingOut: boolean;
  /** The last sign-out the server did not confirm, until one goes through or
   *  the notice is dismissed. */
  signOutFailure: RequestFailure | null;
  /** Sends the sign-out again, for the notice's Try again. */
  retrySignOut: () => Promise<SignOutResult>;
  dismissSignOutFailure: () => void;
  /** Resolves once no sign-out is in flight. A page that opens a session waits
   *  on it before it sends the request. */
  whenSignedOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Same-origin paths only, as _safe_redirect enforces server-side. */
function sameOriginPath(target: unknown): target is string {
  return typeof target === "string" && target.startsWith("/") && !target.startsWith("//");
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const { user, loading, setUser } = useCurrentUser();
  // Whether this server runs a synthetic fleet, as /api/auth/me tells a
  // session. A visitor reads false, which costs nothing deployed: the
  // simulator is on the admin console, which has no visitors, and every
  // deployed /map is real-only, where ground truth is never offered.
  const syntheticFleet = Boolean(user?.synthetic_fleet);
  // Whether the radar registration page's routes answer here.
  const polledRadarRegistration = Boolean(user?.polled_radar_registration);

  const [signingOut, setSigningOut] = useState(false);
  const [signOutFailure, setSignOutFailure] = useState<RequestFailure | null>(null);
  // How many sends in a row the server has left unconfirmed, so the notice can
  // say a retry failed too rather than redraw the same words.
  const [unconfirmed, setUnconfirmed] = useState(0);
  // Counts the sessions signIn has adopted, so a sign-out can tell when it
  // answers whether the session it was sent to end is still the one here.
  const adopted = useRef(0);
  // The sign-out in flight, and the session it was sent to end.
  const inFlight = useRef<{ attempt: Promise<SignOutResult>; session: number } | null>(null);

  // Resolves { redirected, failure } so a caller knows not to route over a
  // navigation that is still in flight. `leave` runs in the same tick the
  // identity is dropped, so a route change it makes renders together with the
  // signed-out state and RequireAuth never judges the old page without a
  // session.
  //
  // The identity is dropped whether or not the server confirms it. A sign-out
  // that timed out or met a 429 still leaves the person expecting to be signed
  // out; keeping the session on screen would read as the click having done
  // nothing, so the notice says what is true instead: this device is signed
  // out, and the server may still hold the session.
  //
  // A session adopted while the request was out is the exception. The request
  // was sent to end the one before, and its answer says nothing about the new
  // one, so that stays, and so does the page it is on, without a notice. The
  // pages that open a session wait for a sign-out in flight before they ask
  // (whenSignedOut), so this is the second line, for a caller that does not.
  const send = useCallback(
    async (leave?: () => void): Promise<SignOutResult> => {
      setSigningOut(true);
      const session = adopted.current;
      let answer: { redirect?: unknown } | null = null;
      let failure: RequestFailure | null = null;
      try {
        answer = await api.logout();
      } catch (err) {
        // A 401 says there is no session to end, which is what was asked for.
        if (!(err instanceof UnauthorizedError)) failure = describeFailure(err);
      }
      const target = answer?.redirect;
      // Only a top-level navigation reaches the edge, and only the edge can end
      // an Access session. Left bare deliberately: the server sends /cdn-cgi/
      // access/logout, an edge path that lives at the origin root, so prefixing
      // it with this bundle's mount would send it to a route that does not exist.
      if (!failure && sameOriginPath(target)) {
        // No setUser: RequireAuth bounces to /login the moment it goes falsy,
        // and the page is leaving anyway. Still signing out until it has gone.
        window.location.assign(target);
        return { redirected: true, failure: null };
      }
      setSigningOut(false);
      if (adopted.current !== session) return { redirected: false, failure };
      setSignOutFailure(failure);
      setUnconfirmed((n) => (failure ? n + 1 : 0));
      try {
        leave?.();
      } finally {
        setUser(null);
      }
      return { redirected: false, failure };
    },
    [setUser],
  );

  // One sign-out at a time: a second call while one is in flight answers with
  // the first rather than sending the request again. Not when a session has
  // been adopted since the first was sent: that one leaves the new session
  // alone, so the new session's own goes out once it has settled.
  const logout = useCallback(
    (leave?: () => void) => {
      const pending = inFlight.current;
      if (pending?.session === adopted.current) return pending.attempt;
      const go = () => send(leave);
      const attempt = pending ? pending.attempt.then(go, go) : go();
      inFlight.current = { attempt, session: adopted.current };
      const settle = () => {
        if (inFlight.current?.attempt === attempt) inFlight.current = null;
      };
      attempt.then(settle, settle);
      return attempt;
    },
    [send],
  );

  // A sign-out's answer deletes the session cookie, whichever session holds it
  // when the answer lands, so one that landed after a sign-in's would end the
  // session the sign-in opened. A page about to open one waits on this first.
  // Looped, since another sign-out may have been queued behind the one waited
  // for.
  const whenSignedOut = useCallback(async () => {
    while (inFlight.current) await inFlight.current.attempt.catch(() => {});
  }, []);

  const retrySignOut = useCallback(() => logout(), [logout]);
  // The count goes with the notice: a later sign-out left unconfirmed is news,
  // not a repeat of one the person has already put away.
  const dismissSignOutFailure = useCallback(() => {
    setSignOutFailure(null);
    setUnconfirmed(0);
  }, []);

  // signIn adopts an identity the caller already holds. The magic-link
  // redemption answers with the user it just signed in, so there is nothing to
  // fetch; without it the guard would bounce a fresh session to the login card
  // until /api/auth/me was asked again. Memoised so it stays referentially
  // stable, which callers may depend on in an effect. A new session settles
  // whatever an earlier sign-out left unconfirmed, so the notice goes with it.
  const signIn = useCallback(
    (next: CurrentUser) => {
      adopted.current += 1;
      setSignOutFailure(null);
      setUnconfirmed(0);
      setUser(next);
    },
    [setUser],
  );

  // A page the back-forward cache restores comes back as it left, which after
  // an Access sign-out is mid-navigation with Sign out still pending.
  useEffect(() => {
    const onShow = (e: PageTransitionEvent) => {
      if (e.persisted) setSigningOut(false);
    };
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      logout,
      signIn,
      syntheticFleet,
      polledRadarRegistration,
      signingOut,
      signOutFailure,
      retrySignOut,
      dismissSignOutFailure,
      whenSignedOut,
    }),
    [
      user,
      loading,
      logout,
      signIn,
      syntheticFleet,
      polledRadarRegistration,
      signingOut,
      signOutFailure,
      retrySignOut,
      dismissSignOutFailure,
      whenSignedOut,
    ],
  );

  // Drawn here rather than by a page, because the person lands wherever the
  // sign-out sends them: an open page on the app host, the login card on the
  // admin console, which has no header to hang it from.
  return (
    <AuthContext.Provider value={value}>
      {children}
      {signOutFailure && (
        <SignOutNotice
          failure={signOutFailure}
          repeated={unconfirmed > 1}
          pending={signingOut}
          onRetry={retrySignOut}
          onDismiss={dismissSignOutFailure}
        />
      )}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
