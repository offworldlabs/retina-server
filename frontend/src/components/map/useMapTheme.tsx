import { createContext, useContext, useEffect, useMemo } from "react";
import {
  activePalette,
  DEFAULT_MAP_THEME,
  PALETTES,
  setActivePalette,
  type MapPalette,
  type MapTheme,
} from "./mapPalette";
import { usePersistedState } from "./usePersistedState";

/**
 * Which palette the map is drawn with, and the switch for it.
 *
 * Dark is the default: it is the surface this map has always had, and the
 * light one is offered rather than imposed.
 *
 * The provider does two things on every change. It publishes the palette to
 * React, for the components that read it through `usePalette`, and it writes
 * the same palette into the module-level slot that icons.ts and the Doppler
 * ramp read — they build Leaflet objects outside the tree and cannot use a
 * hook. Both are driven from one state, so they cannot disagree.
 *
 * It also stamps `data-theme` on the surface element, which is what flips the
 * CSS token block. The chrome needs no JavaScript beyond that: every rule was
 * already written against custom properties.
 */

interface MapThemeValue {
  theme: MapTheme;
  palette: MapPalette;
  setTheme: (t: MapTheme) => void;
  toggleTheme: () => void;
}

const MapThemeContext = createContext<MapThemeValue | null>(null);

export function MapThemeProvider({ children }) {
  const [theme, setTheme] = usePersistedState<MapTheme>("tf.mapTheme", DEFAULT_MAP_THEME);
  const palette = PALETTES[theme] ?? PALETTES[DEFAULT_MAP_THEME];

  // Before paint, so the first render of any consumer already sees the right
  // palette rather than one frame of the default.
  setActivePalette(palette, theme);

  useEffect(() => {
    const el = document.querySelector(".app.map-surface");
    if (el) el.setAttribute("data-theme", theme);
  }, [theme]);

  const value = useMemo<MapThemeValue>(
    () => ({
      theme,
      palette,
      setTheme,
      toggleTheme: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    }),
    [theme, palette, setTheme],
  );

  return <MapThemeContext.Provider value={value}>{children}</MapThemeContext.Provider>;
}

/** The theme and the switch for it. Throws without a provider, because there
 *  is no sensible default for "change the theme". */
export function useMapTheme(): MapThemeValue {
  const ctx = useContext(MapThemeContext);
  if (!ctx) throw new Error("useMapTheme must be used inside MapThemeProvider");
  return ctx;
}

/** The palette alone, which is what almost every consumer wants. Destructure
 *  it and the call sites below read exactly as they did when these were plain
 *  module constants: `const { NODE, SELECTED } = usePalette()`.
 *
 *  Unlike useMapTheme this does NOT require a provider: reading a colour has an
 *  obvious answer without one, and demanding the provider would mean every test
 *  that renders a panel in isolation had to wrap it to ask what shade of amber
 *  a node is. Outside a provider it reports whatever the active palette is. */
export function usePalette(): MapPalette {
  return useContext(MapThemeContext)?.palette ?? activePalette();
}
