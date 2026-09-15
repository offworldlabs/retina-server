import { useResolvedTheme, type Theme } from "../context/ThemeContext";

/**
 * Chart chrome and series colours, per theme.
 *
 * Recharts paints from props rather than from the cascade, so it is the one
 * part of the console a `var()` cannot reach: every axis, grid and tooltip has
 * to be told its colour. These values are therefore a second copy of what
 * App.css already holds, and chartTheme.test.ts asserts the two agree — the
 * same arrangement the stylesheet's two dark blocks are under.
 *
 * The series palette is the guide's categorical ordering for dash. Dark keeps
 * the order and takes each hue one step lighter, because a navy ground admits
 * the light end of a hue where the near-white ground does not.
 */

export interface ChartTheme {
  /** CartesianGrid stroke. */
  grid: string;
  /** XAxis / YAxis stroke, and their tick ink. */
  axis: string;
  /** Recharts wants the tooltip as one style object. */
  tooltip: { background: string; border: string; borderRadius: number; fontSize: number; color: string };
  /** Categorical series, in the guide's order. */
  series: readonly string[];
  /** The "others" slice of a top-N breakdown. Neutral in both themes: it is an
   *  absence of category rather than one more of them. */
  others: string;
}

const TOOLTIP_SHAPE = { borderRadius: 6, fontSize: 12 } as const;

export const CHART_THEMES: Record<Theme, ChartTheme> = {
  light: {
    grid: "#e2e8f0",
    axis: "#94a3b8",
    tooltip: { ...TOOLTIP_SHAPE, background: "#ffffff", border: "1px solid #e2e8f0", color: "#0f172a" },
    series: ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#14b8a6"],
    others: "#94a3b8",
  },
  dark: {
    grid: "rgba(100, 180, 255, 0.14)",
    axis: "#64748b",
    tooltip: {
      ...TOOLTIP_SHAPE,
      background: "#132240",
      border: "1px solid rgba(100, 180, 255, 0.28)",
      color: "#e2e8f0",
    },
    series: ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa", "#f472b6", "#22d3ee", "#a3e635", "#fb923c", "#2dd4bf"],
    others: "#94a3b8",
  },
};

/** The chrome for whichever theme is drawn. Re-reads on a theme change, so a
 *  chart already on screen repaints rather than keeping the old palette. */
export function useChartTheme(): ChartTheme {
  return CHART_THEMES[useResolvedTheme()];
}

/** The series colour for the nth category, wrapping past the tenth rather than
 *  running out and painting SVG's default black. */
export function seriesColour(theme: ChartTheme, index: number): string {
  return theme.series[index % theme.series.length];
}
