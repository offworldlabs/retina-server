import { createContext, useContext, useMemo } from "react";
import { useResolvedTheme } from "../../context/ThemeContext";
import { activePalette, PALETTES, setActivePalette, type MapPalette } from "./mapPalette";

/**
 * Which palette the map is drawn with. The console decides; this publishes the
 * decision to the two places that cannot read a CSS custom property.
 *
 * React gets it through `usePalette`. The module-level slot in mapPalette gets
 * it too, because icons.ts and the Doppler ramp build Leaflet objects outside
 * the tree and cannot use a hook. Both come from one value, so they cannot
 * disagree. The chrome takes the same theme from the `data-theme` MapSurface
 * puts on the surface element.
 */

const MapThemeContext = createContext<MapPalette | null>(null);

export function MapThemeProvider({ children }: { children: React.ReactNode }) {
  const theme = useResolvedTheme();
  const palette = PALETTES[theme];

  // Before paint, so the first render of any consumer sees the right palette
  // rather than one frame of the previous one.
  setActivePalette(palette, theme);

  const value = useMemo(() => palette, [palette]);
  return <MapThemeContext.Provider value={value}>{children}</MapThemeContext.Provider>;
}

/** The palette. Outside a provider it reports whatever is active, so a test
 *  rendering one panel need not wrap it to ask what shade of amber a node is. */
export function usePalette(): MapPalette {
  return useContext(MapThemeContext) ?? activePalette();
}
