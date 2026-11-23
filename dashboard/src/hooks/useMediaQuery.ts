import { useCallback, useSyncExternalStore } from "react";

/**
 * Whether a media query matches now, redrawn when that changes. Reads false
 * where there is no matchMedia, as ThemeContext does, so a page that asks
 * still renders in a test that stubbed none. Pass the queries from
 * styles/breakpoints so the code and the stylesheets switch at the same width.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (notify: () => void) => {
      if (typeof window.matchMedia !== "function") return () => {};
      const list = window.matchMedia(query);
      if (list.addEventListener) {
        list.addEventListener("change", notify);
        return () => list.removeEventListener("change", notify);
      }
      // Safari before 14 has only the deprecated pair.
      list.addListener(notify);
      return () => list.removeListener(notify);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
  );
}
