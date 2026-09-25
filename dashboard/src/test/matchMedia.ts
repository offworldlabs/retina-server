import { act } from "@testing-library/react";
import { vi } from "vitest";

type Listener = (e: MediaQueryListEvent) => void;

/** jsdom has no matchMedia. Installs one answering every query with the OS
 *  colour-scheme preference given, until the file ends or its globals are
 *  unstubbed. Deliberately not in the shared setup: ThemeContext copes with
 *  there being no matchMedia, and a test that does not stub one exercises that. */
export function stubMatchMedia(prefersDark = false) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: prefersDark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: Listener) => listeners.add(fn),
    removeEventListener: (_: string, fn: Listener) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => mql));
  return {
    /** Flip the OS preference the way a real media query would. */
    set(matches: boolean) {
      mql.matches = matches;
      act(() => {
        for (const fn of listeners) fn({ matches } as MediaQueryListEvent);
      });
    },
    listenerCount: () => listeners.size,
  };
}
