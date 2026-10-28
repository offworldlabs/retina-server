import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { stubViewport } from "../../test/matchMedia";
import LiveAircraftMap from "./LiveAircraftMap";
import { MapThemeProvider } from "./useMapTheme";

// On a phone the aircraft list is a sheet over the map (LiveAircraftMap.css,
// layout.phone.test.ts). These cases cover its state: closed on every phone
// visit, and kept apart from the desktop rail's stored choice. A phone is
// 640px wide at most; at 641 the list is the rail again.

// Leaflet layers need a browser layout, as in LiveAircraftMap.test.tsx.
vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  return { ...original, MapContainer: () => <div data-testid="map" /> };
});

vi.mock("../../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, syntheticFleet: false }),
}));

vi.setConfig({ testTimeout: 20_000 });

const STORED = "retina.map.sidebar.collapsed";

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

const list = () => document.querySelector(".aircraft-list-panel") as HTMLElement;
const toggle = () => fireEvent.click(list().querySelector(".al-collapse-btn") as HTMLElement);

function mount() {
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
}

describe("the aircraft list on a phone", () => {
  it("starts closed even where the rail was left open", () => {
    localStorage.setItem(STORED, "false");
    stubViewport(640);
    mount();
    expect(list()).toHaveClass("collapsed");
  });

  it("opens and closes without touching the rail's stored choice", () => {
    stubViewport(390);
    mount();
    toggle();
    expect(list()).not.toHaveClass("collapsed");
    toggle();
    expect(list()).toHaveClass("collapsed");
    expect(localStorage.getItem(STORED)).toBeNull();
  });

  it("follows the window across the breakpoint", () => {
    localStorage.setItem(STORED, "false");
    const viewport = stubViewport(641);
    mount();
    expect(list()).not.toHaveClass("collapsed");

    viewport.resize(640);
    expect(list()).toHaveClass("collapsed");
    toggle();
    expect(list()).not.toHaveClass("collapsed");
    toggle();

    // Back on a wider screen the rail is as it was kept, still open.
    viewport.resize(641);
    expect(list()).not.toHaveClass("collapsed");
    expect(localStorage.getItem(STORED)).toBe("false");
  });
});

describe("the aircraft list on a wider screen", () => {
  it("keeps and stores the rail's choice as before", () => {
    localStorage.setItem(STORED, "true");
    stubViewport(641);
    mount();
    expect(list()).toHaveClass("collapsed");
    toggle();
    expect(list()).not.toHaveClass("collapsed");
    expect(localStorage.getItem(STORED)).toBe("false");
  });
});
