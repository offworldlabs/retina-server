import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { AuthProvider, useAuth } from "../context/AuthContext";
import LiveAircraftMap from "../pages/map/LiveAircraftMap";
import { MapThemeProvider } from "../pages/map/useMapTheme";
import { ownedNode } from "./fixtures/ownedNode";

/**
 * The owner's feed when the session behind it ends. The server closes the
 * owner socket with 1008 then, so the map asks the session through the
 * console's client before it reopens anything: a 401 signs the console out,
 * and the map goes on as the visitor's, on the public feed. The real
 * AuthProvider, client and map are used, with fetch stubbed and the map's
 * Leaflet layers left out.
 */

vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  return { ...original, MapContainer: (_: { children: ReactNode }) => <div data-testid="map" /> };
});

vi.setConfig({ testTimeout: 20_000 });

const ADA = { id: "u-ada", name: "Ada", email: "ada@example.com", role: "user" };
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });

const area = (lat: number, lon: number) => ({
  detection_area: {
    rx: { lat, lon },
    tx: { lat: lat + 0.1, lon: lon + 0.1 },
    beam_azimuth_deg: 90,
    beam_width_deg: 40,
    max_range_km: 50,
  },
});

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** The server. Ada's session holds until `live` goes false, after which every
 *  route behind it answers 401, as it does once it is revoked or expires. */
const server = { live: true };

function route(path: string): Response {
  if (path === "/api/auth/me" || path.startsWith("/api/auth/me/")) {
    if (!server.live) return json(401, { detail: "Not authenticated" });
    if (path === "/api/auth/me") return json(200, ADA);
    if (path === "/api/auth/me/nodes") return json(200, [ROOF]);
    if (path === "/api/auth/me/analytics") return json(200, { nodes: { [ROOF.node_ref!]: area(51.41234, -0.30123) } });
  }
  if (path.startsWith("/api/radar/analytics")) return json(200, { nodes: {} });
  if (path.startsWith("/api/radar/data/")) return json(200, { aircraft: [] });
  return json(404, { detail: `unexpected request: ${path}` });
}

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((event?: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { Socket.instances.push(this); }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
  close() { this.readyState = 3; this.onclose?.(); }
  closedBy(code: number) { this.readyState = 3; this.onclose?.({ code }); }
}

const ownerSockets = () => Socket.instances.filter((s) => s.url.endsWith("/ws/aircraft/owner"));
const paths = () => (fetch as Mock).mock.calls.map(([url]) => String(url));
/** What the session's routes were asked after the first `from` requests. */
const ownerAsks = (from: number) => paths().slice(from).filter((p) => p.startsWith("/api/auth/"));

/** Who the console holds, as every page reads it. */
function Who() {
  const { user } = useAuth();
  return <output aria-label="who">{user?.email ?? "nobody"}</output>;
}

async function advance(ms: number) {
  // In steps, each its own act, so React renders between them.
  for (let step = 0; step < ms; step += 100) {
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  }
}

describe("the owner's feed when the session ends", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    server.live = true;
    Socket.instances = [];
    // An open page, where the client signs the console out in place.
    window.history.replaceState(null, "", "/map");
    vi.stubGlobal("fetch", vi.fn(async (url: string) => route(String(url))));
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  async function ownersMap() {
    render(
      <AuthProvider>
        <Who />
        <MapThemeProvider>
          <LiveAircraftMap feed="real" />
        </MapThemeProvider>
      </AuthProvider>,
    );
    await advance(500);
    expect(screen.getByLabelText("who")).toHaveTextContent(ADA.email);
    fireEvent.click(screen.getByRole("checkbox", { name: "My nodes only" }));
    await advance(500);
    expect(ownerSockets()).toHaveLength(1);
    const socket = ownerSockets()[0];
    act(() => socket.open());
    return socket;
  }

  it("signs the console out on a 1008 the owner list confirms, and never reopens the owner's socket", async () => {
    const socket = await ownersMap();
    const asked = paths().length;

    server.live = false;
    act(() => socket.closedBy(1008));
    // Well inside the 30 s the owner polls wait: the close is what asked.
    await advance(1000);
    expect(screen.getByLabelText("who")).toHaveTextContent("nobody");
    expect(ownerAsks(asked)).toEqual(["/api/auth/me/nodes"]);
    // The visitor's map: no owner toggle, the public feed.
    expect(screen.queryByRole("checkbox", { name: "My nodes only" })).not.toBeInTheDocument();
    const after = Socket.instances.slice(Socket.instances.indexOf(socket) + 1);
    expect(after.map((s) => new URL(s.url).pathname)).toEqual(["/ws/aircraft/live"]);

    // Nor later: ten minutes on, the owner's socket is still the one.
    await advance(600_000);
    expect(ownerSockets()).toHaveLength(1);
    expect(ownerAsks(asked)).toEqual(["/api/auth/me/nodes"]);
  });

  it("reopens the owner's socket after a 1008 the owner list does not confirm", async () => {
    const socket = await ownersMap();
    act(() => socket.closedBy(1008));
    await advance(2900);
    expect(ownerSockets()).toHaveLength(1);
    await advance(200);
    expect(ownerSockets()).toHaveLength(2);
    expect(screen.getByLabelText("who")).toHaveTextContent(ADA.email);
    expect(screen.getByRole("checkbox", { name: "My nodes only" })).toBeChecked();
  });
});
