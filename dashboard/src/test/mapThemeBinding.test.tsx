import { describe, it, expect, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { ThemeProvider } from "../context/ThemeContext";
import { MapThemeProvider, usePalette } from "../pages/map/useMapTheme";
import MapSurface from "../pages/map/MapSurface";
import { PALETTES } from "../pages/map/mapPalette";

/** Seeds storage and stubs matchMedia, which jsdom lacks. The media query keeps
 *  its listeners, so the returned function can flip the OS preference. */
function stubBrowser(seed: Record<string, string>, prefersDark: boolean) {
  for (const [k, v] of Object.entries(seed)) window.localStorage.setItem(k, v);
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  const mql = {
    matches: prefersDark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => listeners.delete(fn),
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
  return (matches: boolean) => {
    mql.matches = matches;
    act(() => {
      for (const fn of listeners) fn({ matches } as MediaQueryListEvent);
    });
  };
}

function Probe({ onPalette }: { onPalette: (n: string) => void }) {
  onPalette(usePalette().NODE);
  return null;
}

function renderUnder(preference: string, prefersDark: boolean) {
  stubBrowser({ "retina.theme": preference }, prefersDark);
  let seen = "";
  const { container } = render(
    <ThemeProvider>
      <MapThemeProvider>
        <MapSurface>
          <Probe onPalette={(n) => { seen = n; }} />
        </MapSurface>
      </MapThemeProvider>
    </ThemeProvider>,
  );
  return { seen, surface: container.querySelector(".map-surface") };
}

describe("the map follows the console's theme", () => {
  it.each([
    ["light", false, "light"],
    ["dark", false, "dark"],
    ["system", true, "dark"],
    ["system", false, "light"],
  ] as const)(
    "console %s (OS dark: %s) draws the %s palette and stamps it on the surface",
    (preference, prefersDark, expected) => {
      const { seen, surface } = renderUnder(preference, prefersDark);
      expect(seen).toBe(PALETTES[expected].NODE);
      expect(surface).toHaveAttribute("data-theme", expected);
    },
  );

  it("follows an OS flip mid-session when the console is on system", () => {
    const flipOs = stubBrowser({ "retina.theme": "system" }, false);
    let seen = "";
    const { container } = render(
      <ThemeProvider>
        <MapThemeProvider>
          <MapSurface>
            <Probe onPalette={(n) => { seen = n; }} />
          </MapSurface>
        </MapThemeProvider>
      </ThemeProvider>,
    );
    const surface = container.querySelector(".map-surface");
    expect(surface).toHaveAttribute("data-theme", "light");
    flipOs(true);
    expect(seen).toBe(PALETTES.dark.NODE);
    expect(surface).toHaveAttribute("data-theme", "dark");
  });
});
