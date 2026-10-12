import { useSyncExternalStore } from "react";
import { vi } from "vitest";

/**
 * A stand-in for AuthContext's useAuth whose session a test can change while a
 * page is on screen, the way the real one changes when /api/auth/me answers or
 * a sign-in adopts a user. Install it with
 *
 *   vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));
 *
 * and call resetAuth() in a beforeEach: the state is module-wide.
 */

export interface FakeUser {
  id?: string;
  email: string;
  name?: string;
}

interface FakeAuthState {
  user: FakeUser | null;
  loading: boolean;
}

const SIGNED_OUT: FakeAuthState = { user: null, loading: false };

let state = SIGNED_OUT;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Changes the session every mounted page sees. Wrap it in act() once a page
 *  is rendered. */
export function setAuth(next: Partial<FakeAuthState>) {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

/** As the real signIn: the page hands over the user its sign-in answered with. */
export const signIn = vi.fn((user: FakeUser) => setAuth({ user, loading: false }));

export const logout = vi.fn(async () => ({ redirected: false }));

export function resetAuth() {
  state = SIGNED_OUT;
  signIn.mockClear();
  logout.mockClear();
}

export function useFakeAuth() {
  const { user, loading } = useSyncExternalStore(subscribe, () => state);
  return { user, loading, signIn, logout };
}
