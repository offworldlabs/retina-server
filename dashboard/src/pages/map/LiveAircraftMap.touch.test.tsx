import { cleanup, render } from "@testing-library/react";
import type { Canvas } from "leaflet";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { stubViewport } from "../../test/matchMedia";
import LiveAircraftMap from "./LiveAircraftMap";
import { DEBUG_PASSIVE_PANE } from "./panes";
import { TouchTapCanvas } from "./touchTapCanvas";
import { MapThemeProvider } from "./useMapTheme";

// The truth dots are drawn on a canvas, which finds the dot under a tap
// itself: within the dot, plus its renderer's `tolerance`. These cases cover
// that reach on a touch screen and with a mouse.

// Every canvas renderer the map makes, of any kind, recorded as its module
// loads. Hoisted above the imports, so the hook is in place before
// LiveAircraftMap makes them.
const canvases = await vi.hoisted(async () => {
  const { default: L } = await import("leaflet");
  const made: Canvas[] = [];
  L.Canvas.addInitHook(function () { made.push(this); });
  return made;
});

// Leaflet layers need a browser layout, as in LiveAircraftMap.test.tsx.
vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  return { ...original, MapContainer: () => <div data-testid="map" /> };
});

vi.mock("../../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, syntheticFleet: false }),
}));

vi.setConfig({ testTimeout: 20_000 });

class Socket {
  static OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  close() { this.readyState = 3; this.onclose?.(); }
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({}) })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount() {
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
}

/** The tolerance shared by every canvas that takes taps, which is each one
 *  outside the pane that ignores the pointer. Each gives a tap to the dot
 *  under it before a neighbour that only reaches it (touchTapCanvas.ts). */
function tapTolerance(): number {
  const tappable = canvases.filter((canvas) => canvas.options.pane !== DEBUG_PASSIVE_PANE);
  expect(tappable.length).toBeGreaterThan(0);
  for (const canvas of tappable) expect(canvas).toBeInstanceOf(TouchTapCanvas);
  const values = new Set(tappable.map((canvas) => canvas.options.tolerance));
  expect(values.size).toBe(1);
  return [...values][0];
}

describe("a truth dot under a finger", () => {
  it("answers a tap from a finger's width away on a touch phone", () => {
    stubViewport(390, { coarse: true });
    mount();
    expect(tapTolerance()).toBe(12);
  });

  it("answers only a tap on the dot with a mouse, on a desktop or in a narrow window", () => {
    stubViewport(1440);
    mount();
    expect(tapTolerance()).toBe(0);
    cleanup();

    stubViewport(390);
    mount();
    expect(tapTolerance()).toBe(0);
  });

  it("follows the pointer and the width as they change", () => {
    // A touch screen as wide as a desktop keeps the desktop's hit test.
    const viewport = stubViewport(1366, { coarse: true });
    mount();
    expect(tapTolerance()).toBe(0);

    viewport.resize(820);
    expect(tapTolerance()).toBe(12);
    viewport.setCoarse(false);
    expect(tapTolerance()).toBe(0);
    viewport.setCoarse(true);
    expect(tapTolerance()).toBe(12);
    viewport.resize(1024);
    expect(tapTolerance()).toBe(0);
  });
});
