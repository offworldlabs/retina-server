import { describe, it, expect } from "vitest";
import rawSurface from "../pages/map/map-surface.css?raw";
import { PALETTES, type MapPalette } from "../pages/map/mapPalette";
import { TOKENS } from "./paletteTokens";

/** The palette's copies of the chrome's tokens, each beside the token it copies. */
const COPIES: [keyof MapPalette, string][] = [
  ["INK_MUTED", "--text-secondary"],
  ["INK_SUBTLE", "--text-muted"],
  ["ACCENT_STRONG", "--accent-hover"],
];

describe.each(["light", "dark"] as const)("the %s map palette", (theme) => {
  // Leaflet's path options and SVG presentation attributes cannot read the
  // cascade, so these are a hand-kept copy of the stylesheet's. Nothing but
  // this test stops the two drifting.
  it.each(COPIES)("holds %s to %s", (key, token) => {
    expect(PALETTES[theme][key]).toBe(TOKENS[theme](token));
  });
});

// The map takes tokens.css as it stands except where its own chrome blocks
// override a token. Overriding one of the copied tokens there would leave the
// comparison above checking against a value the map never draws.
describe("the map's chrome tokens", () => {
  it("override none of the tokens the palette copies", () => {
    const css = rawSurface.replace(/\/\*[\s\S]*?\*\//g, "");
    const overridden = COPIES.map(([, token]) => token).filter((token) => new RegExp(`${token}\\s*:`).test(css));
    expect(overridden).toEqual([]);
  });
});
