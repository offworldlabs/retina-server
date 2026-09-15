import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { ThemeProvider, useTheme, THEME_KEY } from "../context/ThemeContext";
import themeBoot from "../../public/theme-boot.js?raw";

/**
 * Both browser APIs under test are stubbed rather than borrowed from the
 * environment. matchMedia because jsdom has none; localStorage because Node 20
 * and Node 26 disagree about whose implementation `window.localStorage` reaches
 * (26 shadows jsdom's with its own, which throws without `--localstorage-file`),
 * and a suite that passes on CI's Node and fails on a developer's is worse than
 * no suite.
 */
function stubStorage(seed: Record<string, string> = {}) {
  const store = new Map(Object.entries(seed));
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true, writable: true });
  return storage;
}

/** Replace it with one that throws on every access, as private browsing and a
 *  full quota both do. */
function stubBrokenStorage() {
  const boom = () => {
    throw new Error("storage unavailable");
  };
  Object.defineProperty(window, "localStorage", {
    value: { getItem: boom, setItem: boom, removeItem: boom, clear: boom, key: boom, length: 0 },
    configurable: true,
    writable: true,
  });
}

/** Install a matchMedia whose match state the test controls, keeping the
 *  listeners so a test can fire an OS-level theme change. */
function stubMatchMedia(prefersDark: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  const mql = {
    matches: prefersDark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => listeners.delete(fn),
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
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

function Probe() {
  const { preference, resolved, setPreference } = useTheme();
  return (
    <>
      <span data-testid="preference">{preference}</span>
      <span data-testid="resolved">{resolved}</span>
      <button onClick={() => setPreference("dark")}>dark</button>
      <button onClick={() => setPreference("system")}>system</button>
    </>
  );
}

const renderProbe = () => render(<ThemeProvider><Probe /></ThemeProvider>);
const attr = () => document.documentElement.getAttribute("data-theme");

let storage: ReturnType<typeof stubStorage>;

beforeEach(() => {
  storage = stubStorage();
  document.documentElement.removeAttribute("data-theme");
  stubMatchMedia(false);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("theme preference", () => {
  it("defaults to following the OS", () => {
    renderProbe();
    expect(screen.getByTestId("preference")).toHaveTextContent("system");
  });

  it("restores a stored preference", () => {
    storage.setItem(THEME_KEY, "dark");
    renderProbe();
    expect(screen.getByTestId("preference")).toHaveTextContent("dark");
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("persists a change", () => {
    renderProbe();
    act(() => screen.getByText("dark").click());
    expect(storage.getItem(THEME_KEY)).toBe("dark");
  });

  // A value from a future version, a hand-edited one, or another app sharing
  // the origin must not leave the console rendering a theme that has no tokens.
  it("falls back to system on a value it does not recognise", () => {
    storage.setItem(THEME_KEY, "solarized");
    renderProbe();
    expect(screen.getByTestId("preference")).toHaveTextContent("system");
  });

  it("survives storage being unavailable", () => {
    stubBrokenStorage();
    renderProbe();
    expect(screen.getByTestId("preference")).toHaveTextContent("system");
    expect(() => act(() => screen.getByText("dark").click())).not.toThrow();
    expect(screen.getByTestId("preference")).toHaveTextContent("dark");
  });
});

describe("the data-theme attribute", () => {
  // The CSS carries light on the bare selector and dark behind a media query,
  // so "system" must leave no attribute at all rather than stamp a resolved
  // value: stamping one would pin the console to whichever theme the OS
  // happened to be in at load and stop it following a later change.
  it("is absent while the preference is system", () => {
    renderProbe();
    expect(attr()).toBeNull();

    stubMatchMedia(true);
    renderProbe();
    expect(attr()).toBeNull();
  });

  it("is stamped by an explicit preference, and cleared by going back to system", () => {
    renderProbe();
    act(() => screen.getByText("dark").click());
    expect(attr()).toBe("dark");

    act(() => screen.getByText("system").click());
    expect(attr()).toBeNull();
  });
});

describe("the resolved theme", () => {
  it("reports what the OS asks for while the preference is system", () => {
    stubMatchMedia(true);
    renderProbe();
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("follows the OS changing its mind", () => {
    const media = stubMatchMedia(false);
    renderProbe();
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");

    media.set(true);
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  // Charts read `resolved` to pick their chrome, so an explicit choice has to
  // win there as decisively as it does in the CSS.
  it("ignores the OS once the preference is explicit", () => {
    const media = stubMatchMedia(false);
    renderProbe();
    act(() => screen.getByText("dark").click());
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");

    media.set(true);
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
  });

  it("stops listening to the OS when unmounted", () => {
    const media = stubMatchMedia(false);
    const { unmount } = renderProbe();
    expect(media.listenerCount()).toBe(1);
    unmount();
    expect(media.listenerCount()).toBe(0);
  });
});

/**
 * public/theme-boot.js runs before the bundle and stamps the same attribute
 * from the same key, so that a dark console is dark before the first paint
 * rather than after. It cannot import any of this — it is a plain script loaded
 * by a tag, deliberately outside the module graph — so the agreement between
 * the two is asserted here instead.
 */
describe("the pre-paint boot script", () => {
  it("reads the key ThemeContext writes", () => {
    expect(themeBoot).toContain(`"${THEME_KEY}"`);
  });

  it("stamps only the two explicit themes, leaving system to the stylesheet", () => {
    expect(themeBoot).toContain('t === "dark" || t === "light"');
    expect(themeBoot).toContain('setAttribute("data-theme", t)');
    expect(themeBoot).not.toContain('"system"');
  });

  // Reading storage throws outright in some privacy modes, and this runs before
  // anything else on the page — an exception here would take the document with
  // it rather than merely losing the theme.
  it("survives storage throwing", () => {
    expect(themeBoot).toMatch(/try\s*\{[\s\S]*\}\s*catch/);
  });
});
