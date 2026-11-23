import { describe, expect, it, afterEach, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { COARSE_TABLET, DRAWER, PHONE, TABLET } from "../styles/breakpoints";
import { stubViewport } from "./matchMedia";

/* The console changes shape at a fixed set of widths. A stylesheet cannot read
   styles/breakpoints.ts, so a width typed into a rule is a second source of
   truth that nothing else would catch drifting. This reads every stylesheet and
   source file and asks that each width in a media query is one of the set. */

const MAX = new Set(["480px", "640px", "768px", "1023.98px", "1100px", "1200px"]);
const MIN = new Set(["641px", "769px", "1024px"]);

// The map still has its own 600px phone layout and a 900px narrow one.
const MAP_OWN_WIDTHS = { dir: "../pages/map/", max: new Set(["600px", "900px"]) };

const stylesheets = import.meta.glob("../**/*.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const sources = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  ).filter(([file]) => !/^\.\/|^\.\.\/test\/|\.test\.tsx?$/.test(file)),
);

const WIDTH = /\(\s*(max|min)-width\s*:\s*([^)]*?)\s*\)/g;

function strayWidths(file: string, preludes: Iterable<string>): string[] {
  const own = file.startsWith(MAP_OWN_WIDTHS.dir);
  const stray: string[] = [];
  for (const prelude of preludes) {
    for (const [query, kind, value] of prelude.matchAll(WIDTH)) {
      const allowed = kind === "max" ? MAX.has(value) || (own && MAP_OWN_WIDTHS.max.has(value)) : MIN.has(value);
      if (!allowed) stray.push(`${file}: ${query}`);
    }
    // Range syntax such as `width <= 700px` would slip past the check above.
    if (/\bwidth\s*[<>]/.test(prelude)) stray.push(`${file}: ${prelude.trim()}`);
  }
  return stray;
}

function mediaPreludes(css: string): string[] {
  return [...css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/@media([^{]*)\{/g)].map((m) => m[1]);
}

describe("breakpoint widths", () => {
  it("are the only widths the stylesheets switch on", () => {
    const stray = Object.entries(stylesheets).flatMap(([file, css]) => strayWidths(file, mediaPreludes(css)));
    expect(stray).toEqual([]);
  });

  it("are the only widths a source file queries", () => {
    const stray = Object.entries(sources).flatMap(([file, ts]) =>
      strayWidths(file, [...ts.matchAll(/\([^()]*(?:max|min)-width[^()]*\)/g)].map((m) => m[0])),
    );
    expect(stray).toEqual([]);
  });

  it("sees the stylesheets and the queries it is meant to police", () => {
    expect(Object.keys(stylesheets).length).toBeGreaterThan(20);
    const css = (name: string) => Object.entries(stylesheets).find(([file]) => file.endsWith(name))?.[1] ?? "";
    expect(css("/App.css")).toContain("@media (max-width: 768px)");
    expect(css("/App.css")).toContain("@media (min-width: 769px)");
    expect(css("/fleet.css")).toContain("@media (max-width: 640px)");
    expect(Object.entries(sources).find(([file]) => file.endsWith("/styles/breakpoints.ts"))?.[1]).toContain("(max-width: 1023.98px)");
  });
});

describe("the breakpoint queries", () => {
  afterEach(() => vi.unstubAllGlobals());

  // [width, phone, drawer, tablet]
  const widths: [number, boolean, boolean, boolean][] = [
    [320, true, true, true],
    [360, true, true, true],
    [390, true, true, true],
    [640, true, true, true],
    [641, false, true, true],
    [768, false, true, true],
    [769, false, false, true],
    [820, false, false, true],
    [1023, false, false, true],
    [1024, false, false, false],
    [1440, false, false, false],
  ];

  it.each(widths)("at %ipx: phone %s, drawer %s, tablet %s", (width, phone, drawer, tablet) => {
    stubViewport(width);
    const { result } = renderHook(() => [useMediaQuery(PHONE), useMediaQuery(DRAWER), useMediaQuery(TABLET)]);
    expect(result.current).toEqual([phone, drawer, tablet]);
  });

  it("limits the touch tablet query to a coarse pointer", () => {
    stubViewport(820, { coarse: true });
    expect(renderHook(() => useMediaQuery(COARSE_TABLET)).result.current).toBe(true);
    stubViewport(820);
    expect(renderHook(() => useMediaQuery(COARSE_TABLET)).result.current).toBe(false);
  });
});
