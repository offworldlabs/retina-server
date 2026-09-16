import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import LiveAircraftMap from "./LiveAircraftMap";
import { MapThemeProvider } from "./map/useMapTheme";

// Leaflet layers need a browser layout. Keep the real map controller, toolbar,
// playback and feed lifecycle; geometry and layer helpers have their own tests.
vi.mock("react-leaflet", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-leaflet")>(),
  MapContainer: () => <div data-testid="map" />,
}));

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor(public url: string) { Socket.instances.push(this); }
  close() { this.readyState = 3; this.onclose?.(); }
}

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
    ok: true,
    json: async () => url.endsWith("/auth/me")
      ? { email: "owner@example.invalid", name: "Owner" }
      : url.endsWith("/auth/me/nodes") ? [{ node_ref: "mine" }] : {},
  })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("drops paused playback and old history when the owner feed is selected", async () => {
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  act(() => {
    Socket.instances[0].readyState = Socket.OPEN;
    Socket.instances[0].onopen?.();
    Socket.instances[0].onmessage?.({ data: JSON.stringify({
      aircraft: [{ hex: "public", lat: 51, lon: -1 }],
    }) });
  });
  fireEvent.click(screen.getByRole("button", { name: /Pause/ }));
  expect(screen.getByRole("button", { name: /Resume/ })).toBeInTheDocument();
  expect(document.querySelector(".playback-bar")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("checkbox", { name: "My nodes only" }));
  expect(screen.getByRole("checkbox", { name: "My nodes only" })).toBeChecked();
  expect(screen.getByRole("button", { name: /Pause/ })).toBeInTheDocument();
  expect(document.querySelector(".playback-bar")).not.toBeInTheDocument();
  expect(Socket.instances[Socket.instances.length - 1].url).toContain("/ws/aircraft/owner");
});

it("forgets rendered tracks in both directions even before the new feed responds", async () => {
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  const deliver = (flight: string) => {
    act(() => Socket.instances[Socket.instances.length - 1].onmessage?.({ data: JSON.stringify({
      aircraft: [{ hex: "abc123", flight, lat: 51, lon: -1, position_source: "adsb_single_node" }],
    }) }));
    act(() => {
      for (let i = 0; i < 30; i++) {
        const calls = vi.mocked(requestAnimationFrame).mock.calls;
        calls[calls.length - 1][0](i * 16);
      }
    });
  };
  deliver("PUBLIC TRACK");
  expect(screen.getByText("PUBLIC TRACK")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: "My nodes only" }));
  expect(screen.queryByText("PUBLIC TRACK")).not.toBeInTheDocument();
  deliver("OWNER TRACK");
  expect(screen.getByText("OWNER TRACK")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("checkbox", { name: "My nodes only" }));
  expect(screen.queryByText("OWNER TRACK")).not.toBeInTheDocument();
  expect(screen.queryByText("PUBLIC TRACK")).not.toBeInTheDocument();
});
