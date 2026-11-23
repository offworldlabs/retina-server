import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Map as LeafletMap } from "leaflet";
import { Children, isValidElement, type ReactNode } from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import LiveAircraftMap from "../pages/map/LiveAircraftMap";
import { MapThemeProvider } from "../pages/map/useMapTheme";
import { ownedNode } from "./fixtures/ownedNode";

// The deep-link hash against a signed-in owner's own nodes, which the map draws
// at their true position. A fit that frames them lands its centre on that
// position, and the hash mirrors the centre to four decimals, so the fit's view
// must stay out of the hash until the owner moves the map themselves.

vi.hoisted(() => {
  // Leaflet takes its frame scheduler when it loads. One on the fake clock
  // runs the fit's animated pan to its end inside advanceTimersByTime.
  window.requestAnimationFrame = (cb: FrameRequestCallback) => setTimeout(() => cb(Date.now()), 16) as unknown as number;
  window.cancelAnimationFrame = (id: number) => clearTimeout(id);
});

const auth = vi.hoisted(() => ({
  current: { user: null as { id: string; email: string; name: string } | null, loading: false, syntheticFleet: false },
}));
vi.mock("../context/AuthContext", () => ({ useAuth: () => auth.current }));

const live = vi.hoisted(() => ({ map: null as LeafletMap | null }));

// A real Leaflet map holding only what moves it and what writes the hash:
// FitBounds, Follow and HashSync. Every other layer needs a laid-out map.
vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  const kept = new Set(["FitBounds", "FollowController", "HashSync"]);
  const nameOf = (child: ReactNode) => {
    if (!isValidElement(child)) return "";
    const type = child.type as { name?: string; type?: { name?: string } };
    return type.type?.name ?? type.name ?? "";
  };
  function Probe() {
    live.map = original.useMap();
    return null;
  }
  return {
    ...original,
    MapContainer: ({ children, ...props }: { children: ReactNode }) => (
      <original.MapContainer {...props}>
        <Probe />
        {Children.toArray(children).filter((child) => kept.has(nameOf(child)))}
      </original.MapContainer>
    ),
  };
});

vi.setConfig({ testTimeout: 20_000 });

// jsdom lays nothing out; Leaflet reads its container's size from these.
const box = { w: 800, h: 600 };
beforeAll(() => {
  for (const [prop, key] of [["clientWidth", "w"], ["clientHeight", "h"]] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get(this: HTMLElement) { return this.classList.contains("leaflet-container") ? box[key] : 0; },
    });
  }
});
afterAll(() => {
  delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
  delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight;
});

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

const ME = { id: "u1", email: "owner@example.invalid", name: "Owner" };
const PUBLIC_URL = "/api/radar/analytics?real_only=true";

const area = (lat: number, lon: number, rx: object = {}) => ({
  rx: { lat, lon, ...rx },
  tx: { lat: lat + 0.2, lon: lon - 0.1 },
  beam_azimuth_deg: 90,
  beam_width_deg: 40,
  max_range_km: 60,
});

// Someone else's node and the owner's public one, as everybody is served them.
const OTHER = { is_synthetic: false, detection_area: area(51, -1, { location_uncertainty_km: 3 }) };
const ROOF_PUBLISHED = { is_synthetic: false, detection_area: area(52.004, -0.512, { location_uncertainty_km: 3 }) };
// The owner's, where they really are.
const ROOF_AT = { lat: 52.1357, lon: -0.4321 };
const SHED_AT = { lat: 53.1234, lon: -1.5678 };
const ROOF_TRUE = { node_ref: "nde-roof", detection_area: area(ROOF_AT.lat, ROOF_AT.lon) };
const SHED_TRUE = { node_ref: "nde-shed", detection_area: area(SHED_AT.lat, SHED_AT.lon) };
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nde-roof", location_private: false });
const SHED = ownedNode({ node_id: "retshed0001", node_ref: "nde-shed", location_private: true });

type Answer = { ok: boolean; json: () => Promise<unknown> };
const ok = (body: unknown): Answer => ({ ok: true, json: async () => body });

function serve(routes: Record<string, () => Answer>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => (routes[url] ? routes[url]() : ok({}))));
}

/** An owner whose only node is the private shed, and a public listing that
 *  holds nobody else's. */
const onlyTheShed = () => serve({
  [PUBLIC_URL]: () => ok({ nodes: {} }),
  "/api/auth/me/nodes": () => ok([SHED]),
  "/api/auth/me/analytics": () => ok({ nodes: { "nde-shed": SHED_TRUE } }),
});

/** An owner with both nodes, beside somebody else's public one. */
const besideAnother = () => serve({
  [PUBLIC_URL]: () => ok({ nodes: { "nde-other": OTHER, "nde-roof": ROOF_PUBLISHED } }),
  "/api/auth/me/nodes": () => ok([ROOF, SHED]),
  "/api/auth/me/analytics": () => ok({ nodes: { "nde-roof": ROOF_TRUE, "nde-shed": SHED_TRUE } }),
});

const LINK = "/map#lat=40.0000&lon=-100.0000&z=7";
const LINKED = { lat: "40.0000", lon: "-100.0000", z: "7" };

/** Long enough for every answer to land and the fit's half-second pan to end.
 *  In steps, each its own act: React holds the renders and effects an act
 *  starts until it ends, so one long act would only start the fit at its end. */
async function advance(ms = 5000) {
  for (let step = 0; step < ms; step += 100) {
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  }
}

function showMap(props: { feed?: "real" | "synthetic"; ownerView?: boolean } = { feed: "real" }) {
  render(<MapThemeProvider><LiveAircraftMap {...props} /></MapThemeProvider>);
}

/** The view the hash holds, as written. */
function hashView() {
  const p = new URLSearchParams(window.location.hash.slice(1));
  return { lat: p.get("lat"), lon: p.get("lon"), z: p.get("z") };
}

/** The map's view, as the hash would write it. */
function mapView() {
  const c = live.map!.getCenter();
  return { lat: c.lat.toFixed(4), lon: c.lng.toFixed(4), z: String(live.map!.getZoom()) };
}

/** Whether the map is centred on `at`. A pan lands on whole pixels, so
 *  within a pixel at the zoom the map is at. */
function centredOn(at: { lat: number; lon: number }) {
  const c = live.map!.getCenter();
  const pixelDeg = 360 / (256 * 2 ** live.map!.getZoom());
  return Math.abs(c.lat - at.lat) < 2 * pixelDeg && Math.abs(c.lng - at.lon) < 2 * pixelDeg;
}

/** Every URL the hash writer has written so far. */
const written = () => vi.mocked(window.history.replaceState).mock.calls.map((call) => String(call[2]));

/** Whether any hash written so far put the centre within `tolDeg` of `at`. */
function everWroteNear(at: { lat: number; lon: number }, tolDeg = 0.05) {
  return written().some((url) => {
    const p = new URLSearchParams(url.split("#")[1] ?? "");
    const lat = Number(p.get("lat"));
    const lon = Number(p.get("lon"));
    return p.has("lat") && Math.abs(lat - at.lat) < tolDeg && Math.abs(lon - at.lon) < tolDeg;
  });
}

/** A left-button drag across the map, as Map.Drag sees one. jsdom leaves the
 *  legacy `which` at 0, and Leaflet starts a drag only on which or button 1. */
function dragMap(dx: number, dy: number) {
  const el = live.map!.getContainer();
  for (const [type, x, y] of [["mousedown", 400, 300], ["mousemove", 400 + dx, 300 + dy], ["mouseup", 400 + dx, 300 + dy]] as const) {
    const e = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y });
    Object.defineProperty(e, "which", { value: 1 });
    act(() => { el.dispatchEvent(e); });
  }
}

/** A key pressed on the map. Map.Keyboard listens only while the map has
 *  focus. */
function pressKey(init: KeyboardEventInit & { keyCode: number }) {
  const el = live.map!.getContainer();
  act(() => { el.focus(); });
  fireEvent.keyDown(el, init);
}
const ARROW_LEFT = { key: "ArrowLeft", keyCode: 37 };
const PLUS = { key: "+", keyCode: 187 };
const MINUS = { key: "-", keyCode: 189 };

/** Leaflet's own +/- buttons. */
const zoomButton = (which: "in" | "out") =>
  live.map!.getContainer().querySelector(`.leaflet-control-zoom-${which}`) as HTMLElement;

/** An aircraft frame on the feed the map has open. */
function sendAircraft(lat: number, lon: number) {
  const feed = Socket.instances[Socket.instances.length - 1];
  act(() => {
    feed.readyState = Socket.OPEN;
    feed.onopen?.();
    feed.onmessage?.({ data: JSON.stringify({
      now: Date.now() / 1000,
      aircraft: [{ hex: "abc123", flight: "TEST1", lat, lon, position_source: "multinode_solve", alt_baro: 30000, gs: 0, track: 90 }],
    }) });
  });
}

function setUp() {
  vi.useFakeTimers();
  box.w = 800;
  box.h = 600;
  live.map = null;
  Socket.instances = [];
  window.history.replaceState(null, "", "/map");
  vi.spyOn(window.history, "replaceState");
  // Map.Keyboard restores the page's scroll after focusing the map.
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
}

function tearDown() {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
}

describe("the deep-link hash on a signed-in owner's map", () => {
  beforeEach(() => {
    setUp();
    auth.current = { user: ME, loading: false, syntheticFleet: false };
  });
  afterEach(tearDown);

  it.each([
    ["the link it was opened with", LINK, LINKED],
    ["the view it opened on, with no link", "/map", { lat: "34.8500", lon: "-82.3900", z: "9" }],
  ])("keeps %s when the first fit lands on the owner's private node", async (_, url, kept) => {
    window.history.replaceState(null, "", url);
    onlyTheShed();
    showMap();
    await advance();

    // The fit ran, onto the shed's true position, and the hash never had it.
    expect(centredOn(SHED_AT)).toBe(true);
    expect(hashView()).toEqual(kept);
    expect(everWroteNear(SHED_AT)).toBe(false);
  });

  it("holds the fit through a resize and a layer toggle, writes where the owner drags to, and holds an explicit Fit again", async () => {
    window.history.replaceState(null, "", LINK);
    onlyTheShed();
    showMap();
    await advance();
    expect(centredOn(SHED_AT)).toBe(true);
    const fitted = mapView();

    // A panel opening resizes the map, and Leaflet answers with a moveend at
    // the same centre.
    let moveends = 0;
    live.map!.on("moveend", () => { moveends += 1; });
    box.w = 1200;
    act(() => { live.map!.invalidateSize({ animate: false }); });
    await advance(1000);
    expect(moveends).toBe(1);
    expect(mapView()).toMatchObject({ lat: fitted.lat, lon: fitted.lon });
    expect(hashView()).toEqual(LINKED);

    // A toggle rewrites the hash from the last view it recorded, not the fit's.
    const layersBefore = new URLSearchParams(window.location.hash.slice(1)).get("layers");
    fireEvent.click(screen.getByRole("button", { name: "Labels" }));
    await advance(1000);
    expect(new URLSearchParams(window.location.hash.slice(1)).get("layers")).not.toBe(layersBefore);
    expect(hashView()).toEqual(LINKED);

    // The owner's own drag is theirs to share, as it always was.
    dragMap(-160, 90);
    await advance(1000);
    const dragged = mapView();
    expect(dragged.lat).not.toBe(fitted.lat);
    expect(hashView()).toEqual(dragged);

    // Fit takes the map back onto the shed, and that view is held again.
    fireEvent.click(screen.getByRole("button", { name: /Fit/ }));
    await advance();
    expect(centredOn(SHED_AT)).toBe(true);
    expect(hashView()).toEqual(dragged);
    expect(everWroteNear(SHED_AT)).toBe(false);
  });

  const byButton = [() => fireEvent.click(zoomButton("in")), () => fireEvent.click(zoomButton("out"))] as const;
  const byKey = [() => pressKey(PLUS), () => pressKey(MINUS)] as const;
  it.each([
    ["buttons", LINK, byButton, LINKED],
    ["keys", LINK, byKey, LINKED],
    ["buttons", "/map", byButton, { lat: "34.8500", lon: "-82.3900", z: "9" }],
    ["keys", "/map", byKey, { lat: "34.8500", lon: "-82.3900", z: "9" }],
  ])("holds the fit through a zoom in and out with the +/- %s, which keep its centre, opened at %s", async (_, url, [zoomIn, zoomOut], kept) => {
    window.history.replaceState(null, "", url);
    onlyTheShed();
    showMap();
    await advance();
    expect(centredOn(SHED_AT)).toBe(true);
    const fitted = mapView();

    zoomIn();
    await advance(1000);
    expect(Number(mapView().z)).toBe(Number(fitted.z) + 1);
    expect(centredOn(SHED_AT)).toBe(true);
    expect(hashView()).toEqual(kept);

    zoomOut();
    await advance(1000);
    expect(mapView().z).toBe(fitted.z);
    expect(hashView()).toEqual(kept);

    // The zoom is over: a resize after it is no more the owner's than before.
    box.w = 1200;
    act(() => { live.map!.invalidateSize({ animate: false }); });
    await advance(1000);
    expect(hashView()).toEqual(kept);
    expect(everWroteNear(SHED_AT)).toBe(false);
  });

  it.each<[string, boolean, () => void]>([
    ["an arrow key", false, () => pressKey(ARROW_LEFT)],
    ["a zoom, then an arrow key", true, () => pressKey(ARROW_LEFT)],
    ["a zoom, then a drag", true, () => dragMap(-160, 90)],
  ])("writes where the owner pans the held fit to: %s", async (_, zoomFirst, pan) => {
    window.history.replaceState(null, "", LINK);
    onlyTheShed();
    showMap();
    await advance();
    if (zoomFirst) {
      fireEvent.click(zoomButton("in"));
      await advance(1000);
    }
    const before = mapView();
    expect(hashView()).toEqual(LINKED);

    pan();
    await advance(1000);
    const panned = mapView();
    expect(panned.lon).not.toBe(before.lon);
    expect(hashView()).toEqual(panned);
  });

  it("holds every fit on the owner's nodes, before and after they pick My nodes only", async () => {
    window.history.replaceState(null, "", LINK);
    besideAnother();
    showMap();
    await advance();
    // Somebody else's public node and the owner's two true ones, framed together.
    const all = mapView();
    expect(all.lat).not.toBe(LINKED.lat);
    expect(hashView()).toEqual(LINKED);

    fireEvent.click(screen.getByRole("checkbox", { name: "My nodes only" }));
    await advance();
    // A new map, fitted to the two true positions alone.
    const mine = mapView();
    expect(Number(mine.lat)).toBeCloseTo((ROOF_AT.lat + SHED_AT.lat) / 2, 1);
    expect(hashView()).toEqual(LINKED);
    expect(everWroteNear({ lat: Number(all.lat), lon: Number(all.lon) })).toBe(false);
    expect(everWroteNear({ lat: Number(mine.lat), lon: Number(mine.lon) })).toBe(false);
  });

  it("writes a later fit on aircraft, which holds nothing of the owner's", async () => {
    window.history.replaceState(null, "", LINK);
    onlyTheShed();
    showMap();
    await advance();
    expect(hashView()).toEqual(LINKED);

    sendAircraft(50.5, 2.25);
    await advance(1000);
    fireEvent.click(screen.getByRole("button", { name: /Fit/ }));
    await advance();

    expect(centredOn({ lat: 50.5, lon: 2.25 })).toBe(true);
    expect(hashView()).toEqual(mapView());
    expect(everWroteNear(SHED_AT)).toBe(false);
  });

  it("writes the view Follow centres on an aircraft", async () => {
    window.history.replaceState(null, "", LINK);
    onlyTheShed();
    showMap();
    await advance();
    expect(hashView()).toEqual(LINKED);

    sendAircraft(50.5, 2.25);
    await advance(1000);
    fireEvent.click(screen.getByText("TEST1"));
    fireEvent.click(screen.getByRole("button", { name: "Follow" }));
    await advance(1000);

    expect(centredOn({ lat: 50.5, lon: 2.25 })).toBe(true);
    expect(hashView()).toEqual(mapView());
    expect(new URLSearchParams(window.location.hash.slice(1)).get("hex")).toBe("abc123");
    expect(everWroteNear(SHED_AT)).toBe(false);
  });
});

describe("the deep-link hash where no owner's node is on the map", () => {
  beforeEach(setUp);
  afterEach(tearDown);

  it("writes the first fit for a visitor, as it always has", async () => {
    auth.current = { user: null, loading: false, syntheticFleet: false };
    besideAnother();
    showMap();
    await advance();
    expect(mapView().lat).not.toBe("34.8500");
    expect(hashView()).toEqual(mapView());
  });

  it("writes the first fit on /sim, where the page withholds the owner view", async () => {
    auth.current = { user: ME, loading: false, syntheticFleet: true };
    serve({
      "/api/radar/analytics": () => ok({ nodes: { "synth-a": { is_synthetic: true, detection_area: area(40.5, 10.25) } } }),
      "/api/auth/me/nodes": () => ok([SHED]),
      "/api/auth/me/analytics": () => ok({ nodes: { "nde-shed": SHED_TRUE } }),
    });
    showMap({ feed: "synthetic", ownerView: false });
    await advance();
    expect(centredOn({ lat: 40.5, lon: 10.25 })).toBe(true);
    expect(hashView()).toEqual(mapView());
  });
});
