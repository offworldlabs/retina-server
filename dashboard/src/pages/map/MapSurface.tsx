import { useResolvedTheme } from "../../context/ThemeContext";

/**
 * The element the map is drawn in. It carries the console's resolved theme as
 * `data-theme` because the shared palette gives a `.map-surface` without
 * `data-theme="light"` the dark palette whatever the console wears, and the
 * map's own chrome tokens key off the same attribute.
 */
export default function MapSurface({ children }: { children: React.ReactNode }) {
  const theme = useResolvedTheme();
  return (
    <div className="app map-surface map-embedded" data-theme={theme}>
      {children}
    </div>
  );
}
