import { useEffect, useState } from "react";

import { UnauthorizedError, request } from "./request";

/** What `/api/auth/me` answers with. `auth_enabled` is false where the server
 *  admitted the caller without authenticating them, which is the anonymous
 *  admin an environment can opt into. */
export interface CurrentUser {
  id: string;
  email: string;
  name: string;
  avatar?: string;
  provider?: string;
  role?: string;
  is_superuser?: boolean;
  created_at?: number;
  auth_enabled?: boolean;
  /** Whether this server runs a synthetic fleet. */
  synthetic_fleet?: boolean;
  /** Whether this server takes registrations of stock blah2 radars. */
  polled_radar_registration?: boolean;
}

/** The first ask waits on a server that may still be opening its database, so
 *  it is given longer than a request gets by default. A retry is not: by then
 *  the server has woken or it is hanging, and four long waits would hold a
 *  boot in its loading state for two minutes. */
const FIRST_TIMEOUT_MS = 30_000;
const ATTEMPTS = 4;
/** Attempt n waits n × this before attempt n+1, so a run spends ~9 s in total. */
const BACKOFF_STEP_MS = 1500;

export interface CurrentUserState {
  user: CurrentUser | null;
  /** True until the identity is settled, one way or the other. */
  loading: boolean;
  /** Drops the identity locally, for a caller ending its own session. */
  setUser: (user: CurrentUser | null) => void;
}

/** Resolves who the caller is from the shared auth_token cookie, retrying a
 *  server that did not answer and settling a 401 as signed out.
 *
 *  Provider-less on purpose: a surface that renders the map for everyone and
 *  offers an owner view to whoever is signed in wants one component to ask,
 *  not an app-wide context every page pays for. A provider that suits one app
 *  (see the dashboard's) composes over this. */
export function useCurrentUser(): CurrentUserState {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
        if (cancelled) return;
        try {
          const me = await request<CurrentUser>("/api/auth/me", {
            timeoutMs: attempt === 0 ? FIRST_TIMEOUT_MS : undefined,
          });
          if (!cancelled) {
            setUser(me);
            setLoading(false);
          }
          return;
        } catch (e) {
          // A 401 is settled, so stop: the retries are for a busy server, and
          // repeating an unauthenticated call only holds the caller behind a
          // loading state for the length of the backoff.
          if (e instanceof UnauthorizedError) break;
          if (attempt < ATTEMPTS - 1) {
            await new Promise((r) => setTimeout(r, BACKOFF_STEP_MS * (attempt + 1)));
          }
        }
      }
      // Reached by a 401, by four unanswered attempts, or by a body that was
      // not what it should be; none of which is an identity.
      if (!cancelled) {
        setUser(null);
        setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return { user, loading, setUser };
}
