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

type Viewport = { width: number; coarse: boolean; dark: boolean };

/** One media feature, evaluated against the viewport. Unknown features are false. */
function feature(text: string, vp: Viewport): boolean {
  const m = /^\(\s*([\w-]+)\s*:\s*([^)]+?)\s*\)$/.exec(text.trim());
  if (!m) return false;
  const [, name, value] = m;
  switch (name) {
    case "max-width": return vp.width <= parseFloat(value);
    case "min-width": return vp.width >= parseFloat(value);
    case "pointer": return value === (vp.coarse ? "coarse" : "fine");
    case "hover": return value === (vp.coarse ? "none" : "hover");
    case "prefers-color-scheme": return value === (vp.dark ? "dark" : "light");
    default: return false;
  }
}

/** A comma means or, `and` joins features. */
function evaluate(query: string, vp: Viewport): boolean {
  return query.split(",").some((part) => part.split(/\band\b/).every((f) => feature(f, vp)));
}

/** The helper phone and tablet tests use. Installs a matchMedia that answers
 *  every query from a viewport of the given width, and returns the controls
 *  that move it: each notifies the listeners whose answer flipped. */
export function stubViewport(width: number, opts: { coarse?: boolean; dark?: boolean } = {}) {
  const vp: Viewport = { width, coarse: opts.coarse ?? false, dark: opts.dark ?? false };
  const lists = new Map<string, { matches: boolean; listeners: Set<Listener> }>();
  const make = (query: string) => {
    let state = lists.get(query);
    if (!state) {
      state = { matches: evaluate(query, vp), listeners: new Set<Listener>() };
      lists.set(query, state);
    }
    const { listeners } = state;
    const add = (_: string, fn: Listener) => void listeners.add(fn);
    const remove = (_: string, fn: Listener) => void listeners.delete(fn);
    return {
      get matches() { return lists.get(query)!.matches; },
      media: query,
      addEventListener: add,
      removeEventListener: remove,
      addListener: (fn: Listener) => add("change", fn),
      removeListener: (fn: Listener) => remove("change", fn),
    };
  };
  vi.stubGlobal("matchMedia", vi.fn(make));
  const refresh = () => {
    const flipped = [...lists].filter(([query, s]) => s.matches !== evaluate(query, vp));
    act(() => {
      for (const [query, s] of flipped) {
        s.matches = !s.matches;
        for (const fn of s.listeners) fn({ matches: s.matches, media: query } as MediaQueryListEvent);
      }
    });
  };
  return {
    resize(next: number) { vp.width = next; refresh(); },
    setCoarse(next: boolean) { vp.coarse = next; refresh(); },
    setDark(next: boolean) { vp.dark = next; refresh(); },
    /** Listeners now attached across every query. */
    listenerCount: () => [...lists.values()].reduce((n, s) => n + s.listeners.size, 0),
  };
}
