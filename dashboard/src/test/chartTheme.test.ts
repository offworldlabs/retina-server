import { describe, it, expect } from "vitest";
import { CHART_THEMES, seriesColour } from "../utils/chartTheme";
import { TOKENS } from "./paletteTokens";

describe.each(["light", "dark"] as const)("the %s chart chrome", (theme) => {
  const chart = CHART_THEMES[theme];
  const tokenFor = TOKENS[theme];

  // Recharts cannot read the cascade, so these values are a hand-kept copy of
  // the stylesheet's. Nothing but this test stops the two drifting.
  it("draws its grid in the border colour", () => {
    expect(chart.grid).toBe(tokenFor("--border"));
  });

  it("draws its axes in the muted ink", () => {
    expect(chart.axis).toBe(tokenFor("--text-muted"));
  });

  it("draws its tooltip as a card", () => {
    expect(chart.tooltip.background).toBe(tokenFor("--bg-card"));
    expect(chart.tooltip.color).toBe(tokenFor("--text-primary"));
  });
});

// A tooltip floats over a chart that is itself on a card, so its border is the
// only thing separating the two — and the themes need different tokens to get
// the same separation. Light's hairline is a solid grey that reads against
// white; dark's is a 14%-alpha white that does not read against the navy it is
// drawn on, so dark takes the stronger one.
describe("the tooltip border", () => {
  it("is the hairline on light", () => {
    expect(CHART_THEMES.light.tooltip.border).toBe(`1px solid ${TOKENS.light("--border")}`);
  });

  it("is the stronger hairline on dark", () => {
    expect(CHART_THEMES.dark.tooltip.border).toBe(`1px solid ${TOKENS.dark("--border-light")}`);
  });
});

/**
 * The stylesheet has themeTokens.test.ts sweeping it for stray colours; the
 * pages need their own sweep, because a chart's colours are props rather than
 * CSS and no token block covers them. A literal here is pinned to one theme and
 * mismatches its own neighbours in the other — an `Area` whose stroke follows
 * the palette and whose fill does not is the shape this catches.
 */
describe("the chart pages", () => {
  const ALLOWED = [
    // Painted onto OSM tiles, which stay light in both themes.
    "src/pages/admin/NetworkHealthPage.tsx",
    // Its own documented domain palette, with a value per theme.
    "src/pages/user/AnomalyPage.tsx",
  ];

  const pages = import.meta.glob("../pages/**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

  it("finds pages to check, so a glob that matched nothing cannot pass", () => {
    expect(Object.keys(pages).length).toBeGreaterThan(10);
  });

  it.each(Object.keys(pages).filter((p) => !ALLOWED.some((a) => p.endsWith(a.replace("src/", "")))))(
    "%s hardcodes no colour",
    (path) => {
      const source = pages[path].replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
      expect(source.match(/#[0-9a-f]{3,8}\b|\brgba?\([\d\s,.]+\)/gi) ?? []).toEqual([]);
    },
  );

  // A named colour is as pinned to one theme as a hex is, and an inline style
  // outranks the class rule it sits on — `color: "white"` on a .btn-primary
  // quietly defeats --accent-ink. `transparent` and `currentColor` are fine.
  it.each(Object.keys(pages))("%s names no colour in a style object", (path) => {
    const source = pages[path].replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "");
    const named = /\b(?:color|background|backgroundColor|borderColor|fill|stroke)\s*:\s*"(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)"/gi;
    expect(source.match(named) ?? []).toEqual([]);
  });
});

describe("the series palette", () => {
  // The guide fixes this ordering for dash's charts; dark keeps it and takes
  // each hue a step lighter, so the same category is the same position and
  // recognisably the same hue in either theme.
  it("is the guide's order in light", () => {
    expect(CHART_THEMES.light.series).toEqual([
      "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
      "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#14b8a6",
    ]);
  });

  it("holds the same number of categories in both themes", () => {
    expect(CHART_THEMES.dark.series).toHaveLength(CHART_THEMES.light.series.length);
  });

  it("shares no colour between the two themes, so neither is half-ported", () => {
    const shared = CHART_THEMES.dark.series.filter((c) => CHART_THEMES.light.series.includes(c));
    expect(shared).toEqual([]);
  });

  it("wraps past the last category rather than running out", () => {
    const light = CHART_THEMES.light;
    expect(seriesColour(light, 0)).toBe(light.series[0]);
    expect(seriesColour(light, 10)).toBe(light.series[0]);
    expect(seriesColour(light, 13)).toBe(light.series[3]);
  });
});
