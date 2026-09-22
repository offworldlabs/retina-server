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
  /** The Anomaly Monitor's categories, keyed by the backend's reason. */
  anomaly: AnomalyPalette;
}

/**
 * A domain palette, not the status ramp: these hues say which kind of anomaly,
 * not how bad it is. Dark keeps every hue and takes it one step lighter, the
 * same move the series makes, so a type is recognisably itself in either theme.
 */
export interface AnomalyPalette {
  types: Readonly<Record<string, string>>;
  /** For a reason the backend added before this palette did. */
  fallback: string;
  /** Text on a badge filled with one of the colours above. White is unreadable
   *  on the lighter dark variants. */
  ink: string;
}

const TOOLTIP_SHAPE = { borderRadius: 6, fontSize: 12 } as const;

export const CHART_THEMES: Record<Theme, ChartTheme> = {
  light: {
    grid: "#e2e8f0",
    axis: "#94a3b8",
    tooltip: { ...TOOLTIP_SHAPE, background: "#ffffff", border: "1px solid #e2e8f0", color: "#0f172a" },
    series: ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#14b8a6"],
    others: "#94a3b8",
    anomaly: {
      types: {
        supersonic: "#ef4444",
        instant_acceleration: "#f97316",
        instant_direction_change: "#eab308",
        sustained_orbit: "#8b5cf6",
        position_mismatch: "#3b82f6",
        identity_swap: "#ec4899",
        altitude_jump: "#14b8a6",
        anomalous_behavior: "#6b7280",
      },
      fallback: "#6b7280",
      ink: "#ffffff",
    },
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
    anomaly: {
      types: {
        supersonic: "#f87171",
        instant_acceleration: "#fb923c",
        instant_direction_change: "#facc15",
        sustained_orbit: "#a78bfa",
        position_mismatch: "#60a5fa",
        identity_swap: "#f472b6",
        altitude_jump: "#2dd4bf",
        anomalous_behavior: "#9ca3af",
      },
      fallback: "#9ca3af",
      ink: "#0b1220",
    },
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

/** The colour for an anomaly type, or the fallback for one the palette does not
 *  know. */
export function anomalyColour(theme: ChartTheme, type: string | undefined): string {
  const { types, fallback } = theme.anomaly;
  // hasOwnProperty, not a plain lookup: these keys come from the backend, and
  // `constructor` or `toString` would otherwise resolve up the prototype chain
  // and hand Recharts a function as a colour.
  return type && Object.prototype.hasOwnProperty.call(types, type) ? types[type] : fallback;
}
