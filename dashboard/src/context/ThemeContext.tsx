import { createContext, useContext, useEffect, useMemo, useState } from "react";

/**
 * Which palette the console is drawn with, and the switch for it.
 *
 * The mechanism is the map surface's (`.map-surface` in @retina/shared's
 * tokens.css), with the two themes the other way round. The map carries dark on
 * its base selector and light behind the attribute, because dark was its
 * default and the default must never be the theme that flashes. Here light is
 * the default, so light is what the bare selector holds and dark is what the
 * attribute buys.
 *
 * The third state is the reason this is not a boolean. `system`
 * stamps no attribute at all and lets the `prefers-color-scheme` block in
 * App.css answer, which means the OS preference is honoured with no JavaScript
 * and keeps working when the OS changes its mind mid-session. Stamping a
 * resolved value instead would pin the console to whatever the OS happened to
 * be at load.
 *
 * `resolved` exists for the one thing CSS cannot reach: Recharts paints to a
 * canvas from props, so useChartTheme needs to be told which palette is on
 * screen rather than inheriting it.
 */

export type ThemePreference = "system" | "light" | "dark";
export type Theme = "light" | "dark";

export const THEME_KEY = "retina.theme";
export const DARK_QUERY = "(prefers-color-scheme: dark)";

const PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"];

function isPreference(v: unknown): v is ThemePreference {
  return typeof v === "string" && (PREFERENCES as readonly string[]).includes(v);
}

/** The stored preference, or `system` for anything this version cannot read —
 *  a value from a future one, a hand-edited key, or no storage at all. */
function storedPreference(): ThemePreference {
  try {
    const raw = window.localStorage.getItem(THEME_KEY);
    return isPreference(raw) ? raw : "system";
  } catch {
    return "system";
  }
}

/** Absent in jsdom and in any non-browser render, so every caller has to cope
 *  with there being no media query to ask. */
function darkQuery(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(DARK_QUERY) : null;
}

interface ThemeValue {
  preference: ThemePreference;
  /** The theme actually on screen, with `system` already resolved. */
  resolved: Theme;
  setPreference: (p: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(storedPreference);
  const [systemDark, setSystemDark] = useState(() => darkQuery()?.matches ?? false);

  useEffect(() => {
    const mql = darkQuery();
    if (!mql) return;
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  const resolved: Theme = preference === "system" ? (systemDark ? "dark" : "light") : preference;

  useEffect(() => {
    const root = document.documentElement;
    if (preference === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", preference);
  }, [preference]);

  const setPreference = (p: ThemePreference) => {
    setPreferenceState(p);
    try {
      window.localStorage.setItem(THEME_KEY, p);
    } catch {
      /* quota exceeded / private mode — the choice still holds for this tab */
    }
  };

  const value = useMemo<ThemeValue>(
    () => ({ preference, resolved, setPreference }),
    [preference, resolved],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/** Throws without a provider, because there is no sensible default for
 *  "change the theme". Read-only consumers want useResolvedTheme instead. */
export function useTheme(): ThemeValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider");
  return ctx;
}

/** The theme alone, which is what the charts want. Unlike useTheme this does
 *  not require a provider: asking which palette is drawn has an obvious answer
 *  without one, and demanding it would mean every test rendering a chart in
 *  isolation had to wrap it. */
export function useResolvedTheme(): Theme {
  const ctx = useContext(ThemeContext);
  if (ctx) return ctx.resolved;
  return document.documentElement.getAttribute("data-theme") === "dark" ||
    (!document.documentElement.hasAttribute("data-theme") && (darkQuery()?.matches ?? false))
    ? "dark"
    : "light";
}
