import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The key is read once, at module load, so a stubbed env only takes effect on
 * a fresh import — hence resetModules() before each dynamic import rather than
 * a plain top-level one.
 */
async function withKey(key: string) {
  vi.resetModules();
  vi.stubEnv("VITE_CARTO_API_KEY", key);
  return (await import("./basemap")).withCartoKey;
}

const CARTO = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png";
const OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("withCartoKey", () => {
  it("appends the key to Carto URLs", async () => {
    const f = await withKey("k123");
    expect(f(CARTO)).toBe(`${CARTO}?api_key=k123`);
  });

  it("leaves the URL untouched when no key was built in", async () => {
    // The deployed default on any host without a key: identical to what
    // shipped before this module existed.
    const f = await withKey("");
    expect(f(CARTO)).toBe(CARTO);
  });

  it("treats a whitespace-only key as no key", async () => {
    const f = await withKey("   ");
    expect(f(CARTO)).toBe(CARTO);
  });

  it("never keys a non-Carto basemap", async () => {
    // The map's layer switcher hands OSM URLs through the same call.
    const f = await withKey("k123");
    expect(f(OSM)).toBe(OSM);
  });

  it("uses & when the URL already carries a query", async () => {
    const f = await withKey("k123");
    expect(f(`${CARTO}?foo=1`)).toBe(`${CARTO}?foo=1&api_key=k123`);
  });
});
