import { act, cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { Children, isValidElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PRIVATE_DESCRIPTION } from "../components/LocationPrivacy";
import LiveAircraftMap from "../pages/map/LiveAircraftMap";
import { useNodes } from "../pages/map/hooks";
import { MapThemeProvider } from "../pages/map/useMapTheme";
import { ownedNode } from "./fixtures/ownedNode";

// The map's node set for a signed-in owner. The public analytics listing is
// the same for every caller and leaves every private node out, the owner's
// included, so the owner's own nodes come from the owner API and take the
// place of their public entries.

const auth = vi.hoisted(() => ({
  current: { user: null as { id: string; email: string; name: string } | null, loading: false, syntheticFleet: false },
}));
vi.mock("../context/AuthContext", () => ({ useAuth: () => auth.current }));

// The map is rendered whole, but only its node markers are drawn, through
// pass-throughs: every other layer needs a laid-out Leaflet map.
vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  const isNodeMarkers = (child: ReactNode) =>
    isValidElement(child) && (child.type as { type?: { name?: string } })?.type?.name === "NodeMarkersLayer";
  return {
    ...original,
    MapContainer: ({ children }: { children: ReactNode }) => (
      <div data-testid="map">{Children.toArray(children).filter(isNodeMarkers)}</div>
    ),
    Marker: ({ position, children }: { position: [number, number]; children: ReactNode }) => (
      <div data-testid="node-site" data-position={JSON.stringify(position)}>{children}</div>
    ),
    Popup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    Circle: () => <span data-testid="uncertainty-disc" />,
  };
});

vi.setConfig({ testTimeout: 20_000 });

const ME = { id: "u1", email: "owner@example.invalid", name: "Owner" };
const PUBLIC_URL = "/api/radar/analytics?real_only=true";

const area = (lat: number, lon: number, rx: object = {}) => ({
  rx: { lat, lon, ...rx },
  tx: { lat: lat + 0.2, lon: lon - 0.1 },
  beam_azimuth_deg: 90,
  beam_width_deg: 40,
  max_range_km: 60,
});

// Someone else's node, and the owner's public one as everybody is served it:
// displaced, with the radius declared.
const OTHER = { is_synthetic: false, detection_area: area(51, -1, { location_uncertainty_km: 3 }) };
const ROOF_PUBLISHED = {
  is_synthetic: false,
  detection_area: area(52.004, -0.512, { location_uncertainty_km: 3 }),
  empirical_coverage: { polygon: [[52.004, -0.512], [52.304, -0.412], [52.104, -0.212]], n_points: 12, polygon_source: "evidence" },
};
const PUBLIC = { nodes: { "nde-other": OTHER, "nde-roof": ROOF_PUBLISHED } };

// The owner's, with nothing moved: no radius, true receiver, true polygon.
const ROOF_TRUE = {
  node_id: "retroof0001",
  node_ref: "nde-roof",
  trust: {}, metrics: {}, reputation: {}, coverage_map: null, node_bias: null,
  detection_area: area(52, -0.5),
  empirical_coverage: { polygon: [[52, -0.5], [52.3, -0.4], [52.1, -0.2]], n_points: 12, polygon_source: "evidence" },
};
const SHED_TRUE = {
  node_id: "retshed0001",
  node_ref: "nde-shed",
  trust: {}, metrics: {}, reputation: {}, coverage_map: null, empirical_coverage: null, node_bias: null,
  detection_area: area(53.1234, -1.5678),
};
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nde-roof", location_private: false });
const SHED = ownedNode({ node_id: "retshed0001", node_ref: "nde-shed", location_private: true });
const OWNED = [ROOF, SHED];
const OWNER_ANALYTICS = { nodes: { "nde-roof": ROOF_TRUE, "nde-shed": SHED_TRUE } };

type Answer = { ok: boolean; status?: number; statusText?: string; json: () => Promise<unknown> };
const ok = (body: unknown): Answer => ({ ok: true, json: async () => body });
const failed = (status: number): Answer => ({ ok: false, status, statusText: "", json: async () => ({ detail: "boom" }) });

/** One path's answer; a promise holds it back until the test settles it. */
type Route = () => Answer | Promise<Answer>;

/** Answers each path from `routes`, by exact path and query; anything else is
 *  an empty object. */
function serve(routes: Record<string, Route>) {
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => (routes[url] ? routes[url]() : ok({})));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The public listing, and the owner API answering with `nodes` and `analytics`. */
const ownerRoutes = (nodes: Route = () => ok(OWNED), analytics: Route = () => ok(OWNER_ANALYTICS)) => ({
  [PUBLIC_URL]: () => ok(PUBLIC),
  "/api/auth/me/nodes": nodes,
  "/api/auth/me/analytics": analytics,
});

const urls = (fetchMock: ReturnType<typeof serve>) => fetchMock.mock.calls.map(([url]) => String(url));
const ownerRequests = (fetchMock: ReturnType<typeof serve>) => urls(fetchMock).filter((u) => u.startsWith("/api/auth/me"));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

/** The public listing as useNodes drew it before any owner was involved. */
const PUBLIC_NODES = [
  expect.objectContaining({ node_ref: "nde-other", rx_lat: 51, rx_lon: -1, location_uncertainty_km: 3 }),
  expect.objectContaining({ node_ref: "nde-roof", rx_lat: 52.004, rx_lon: -0.512, location_uncertainty_km: 3 }),
];

describe("the node set useNodes gives the map", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function settle() {
    for (let i = 0; i < 3; i++) await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  }

  /** What a visitor's map holds, for comparing an owner's against. */
  async function signedOutNodes() {
    serve(ownerRoutes());
    const { result, unmount } = renderHook(() => useNodes("real", null));
    await settle();
    const nodes = result.current;
    unmount();
    return nodes;
  }

  it("adds the owner's private node at its true receiver position, marked private", async () => {
    const fetchMock = serve(ownerRoutes());
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    const shed = result.current.find((n) => n.node_ref === "nde-shed");
    expect(shed).toEqual({
      node_ref: "nde-shed",
      rx_lat: 53.1234,
      rx_lon: -1.5678,
      location_uncertainty_km: 0,
      tx_lat: 53.1234 + 0.2,
      tx_lon: -1.5678 - 0.1,
      rx_alt_m: null,
      tx_alt_m: null,
      beam_azimuth_deg: 90,
      beam_width_deg: 40,
      max_range_km: 60,
      max_bistatic_range_km: null,
      empirical_polygon: null,
      empirical_n_points: 0,
      empirical_polygon_source: "evidence",
      is_synthetic: false,
      location_private: true,
    });
    // The public route is asked exactly as before, and nothing of the owner's
    // goes to it: the owner's nodes are read from the owner API alone.
    expect(urls(fetchMock).sort()).toEqual(["/api/auth/me/analytics", "/api/auth/me/nodes", PUBLIC_URL]);
    for (const [url, init] of fetchMock.mock.calls) {
      if (!String(url).startsWith("/api/radar")) continue;
      expect(init?.method).toBeUndefined();
      expect(init?.body).toBeUndefined();
    }
  });

  it("draws an owned public node once, at its true position instead of the displaced one", async () => {
    serve(ownerRoutes());
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    expect(result.current.map((n) => n.node_ref)).toEqual(["nde-other", "nde-roof", "nde-shed"]);
    const roof = result.current.filter((n) => n.node_ref === "nde-roof");
    expect(roof).toHaveLength(1);
    expect(roof[0]).toMatchObject({
      rx_lat: 52,
      rx_lon: -0.5,
      location_uncertainty_km: 0,
      empirical_polygon: ROOF_TRUE.empirical_coverage.polygon,
      location_private: false,
    });
    // Somebody else's node is as the public listing served it.
    expect(result.current[0]).toEqual(PUBLIC_NODES[0]);
    expect(result.current[0]).not.toHaveProperty("location_private");
  });

  it("is the public listing, and asks the owner API nothing, when nobody is signed in", async () => {
    const fetchMock = serve(ownerRoutes());
    const { result } = renderHook(() => useNodes("real", null));
    await settle();
    expect(result.current).toEqual(PUBLIC_NODES);
    expect(result.current.some((n) => "location_private" in n)).toBe(false);
    expect(ownerRequests(fetchMock)).toEqual([]);
    expect(urls(fetchMock)).toEqual([PUBLIC_URL]);
  });

  it("is the public listing for an owner with no nodes", async () => {
    const visitor = await signedOutNodes();
    serve(ownerRoutes(() => ok([]), () => ok({ nodes: {} })));
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    expect(result.current).toEqual(visitor);
    expect(result.current).toEqual(PUBLIC_NODES);
  });

  it.each([
    ["its node list fails", () => failed(500), () => ok(OWNER_ANALYTICS)],
    ["its analytics fail", () => ok(OWNED), () => failed(503)],
    ["the session has lapsed", () => failed(401), () => failed(401)],
    ["the network is down", () => Promise.reject(new TypeError("Failed to fetch")), () => Promise.reject(new TypeError("Failed to fetch"))],
  ])("falls back to the public listing when the owner API fails: %s", async (_, nodes, analytics) => {
    const visitor = await signedOutNodes();
    serve(ownerRoutes(nodes, analytics));
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    expect(result.current).toEqual(visitor);
  });

  it("keeps the owner's nodes through a failed refresh, as it keeps the public ones", async () => {
    let analytics = () => ok(OWNER_ANALYTICS);
    const fetchMock = serve(ownerRoutes(() => ok(OWNED), () => analytics()));
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    analytics = () => failed(500);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    await settle();
    expect(urls(fetchMock).filter((u) => u === "/api/auth/me/analytics")).toHaveLength(2);
    expect(result.current.map((n) => n.node_ref)).toEqual(["nde-other", "nde-roof", "nde-shed"]);
  });

  it("drops the owner's nodes in the same render the owner signs out", async () => {
    serve(ownerRoutes());
    const { result, rerender } = renderHook(({ owner }) => useNodes("real", owner), {
      initialProps: { owner: ME as typeof ME | null },
    });
    await settle();
    expect(result.current.map((n) => n.node_ref)).toContain("nde-shed");
    rerender({ owner: null });
    expect(result.current).toEqual(PUBLIC_NODES);
  });

  it("never shows one owner's nodes to the next while the next one's answer is on its way", async () => {
    const next = deferred<Answer>();
    let nodes: Route = () => ok(OWNED);
    serve(ownerRoutes(() => nodes(), () => ok(OWNER_ANALYTICS)));
    const { result, rerender } = renderHook(({ owner }) => useNodes("real", owner), {
      initialProps: { owner: ME as { id: string } },
    });
    await settle();
    expect(result.current.map((n) => n.node_ref)).toContain("nde-shed");
    nodes = () => next.promise;
    rerender({ owner: { id: "u2" } });
    expect(result.current).toEqual(PUBLIC_NODES);
    await settle();
    expect(result.current).toEqual(PUBLIC_NODES);
    await act(async () => next.resolve(ok([])));
    await settle();
    expect(result.current).toEqual(PUBLIC_NODES);
  });

  it("keeps an owner's synthetic node off a real-fleet page", async () => {
    const bot = ownedNode({ node_id: "synth-bot01", node_ref: "synth-bot01", is_synthetic: true, location_private: true });
    serve(ownerRoutes(
      () => ok([...OWNED, bot]),
      () => ok({ nodes: { ...OWNER_ANALYTICS.nodes, "synth-bot01": { ...SHED_TRUE, node_ref: "synth-bot01", detection_area: area(40, 10) } } }),
    ));
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    expect(result.current.map((n) => n.node_ref)).toEqual(["nde-other", "nde-roof", "nde-shed"]);
  });

  it("leaves a node released between the two owner answers as the public listing has it", async () => {
    // The analytics still hold the roof; the node list no longer does.
    serve(ownerRoutes(() => ok([SHED]), () => ok(OWNER_ANALYTICS)));
    const { result } = renderHook(() => useNodes("real", ME));
    await settle();
    expect(result.current.map((n) => n.node_ref)).toEqual(["nde-other", "nde-roof", "nde-shed"]);
    expect(result.current[1]).toEqual(PUBLIC_NODES[1]);
  });
});

describe("the map an owner sees", () => {
  class Socket {
    static OPEN = 1;
    readyState = 0;
    onopen: (() => void) | null = null;
    onclose: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    constructor(public url: string) {}
    close() { this.readyState = 3; this.onclose?.(); }
  }

  beforeEach(() => {
    auth.current = { user: ME, loading: false, syntheticFleet: false };
    window.history.replaceState(null, "", "/");
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const siteAt = (lat: number, lon: number) =>
    screen.queryAllByTestId("node-site").filter((s) => s.getAttribute("data-position") === JSON.stringify([lat, lon]));

  it("marks the private node in its popup, at its true position, and draws the public one there once", async () => {
    serve(ownerRoutes());
    render(<MapThemeProvider><LiveAircraftMap feed="real" /></MapThemeProvider>);
    const mark = await screen.findByText("Private: hidden from the public map");
    expect(mark).toHaveAttribute("title", PRIVATE_DESCRIPTION);

    const [shed] = siteAt(53.1234, -1.5678);
    expect(within(shed).getByText("nde-shed")).toBeInTheDocument();
    expect(within(shed).getByText("Private: hidden from the public map")).toBeInTheDocument();

    const [roof] = siteAt(52, -0.5);
    expect(within(roof).getByText("nde-roof")).toBeInTheDocument();
    expect(within(roof).queryByText(/Private/)).not.toBeInTheDocument();
    expect(siteAt(52.004, -0.512)).toEqual([]);
    expect(screen.getAllByText("nde-roof")).toHaveLength(1);

    // Only somebody else's node is approximate: the owner's two are where
    // they are.
    expect(screen.getAllByTestId("node-site")).toHaveLength(3);
    expect(screen.getAllByTestId("uncertainty-disc")).toHaveLength(1);
    expect(screen.getAllByText(/Location approximate/)).toHaveLength(1);
  });

  it("keeps the private node in the owner's own view", async () => {
    serve(ownerRoutes());
    render(<MapThemeProvider><LiveAircraftMap feed="real" /></MapThemeProvider>);
    await screen.findByText("Private: hidden from the public map");
    fireEvent.click(await screen.findByRole("checkbox", { name: "My nodes only" }));
    await screen.findByText("Private: hidden from the public map");
    expect(screen.getAllByTestId("node-site").map((s) => s.getAttribute("data-position")).sort()).toEqual(
      [JSON.stringify([52, -0.5]), JSON.stringify([53.1234, -1.5678])],
    );
  });

  it("is the public map for a visitor, and asks the owner API nothing", async () => {
    auth.current = { user: null, loading: false, syntheticFleet: false };
    const fetchMock = serve(ownerRoutes());
    render(<MapThemeProvider><LiveAircraftMap feed="real" /></MapThemeProvider>);
    await screen.findByText("nde-roof");
    expect(siteAt(52.004, -0.512)).toHaveLength(1);
    expect(screen.queryByText("nde-shed")).not.toBeInTheDocument();
    expect(screen.queryByText(/Private/)).not.toBeInTheDocument();
    expect(screen.getAllByTestId("uncertainty-disc")).toHaveLength(2);
    expect(ownerRequests(fetchMock)).toEqual([]);
  });

  it("asks the owner API nothing where the page withholds the owner view", async () => {
    // The admin console's /sim, under a signed-in session.
    const fetchMock = serve({
      "/api/radar/analytics": () => ok({ nodes: {} }),
      "/api/auth/me/nodes": () => ok(OWNED),
      "/api/auth/me/analytics": () => ok(OWNER_ANALYTICS),
    });
    render(<MapThemeProvider><LiveAircraftMap feed="synthetic" ownerView={false} /></MapThemeProvider>);
    await screen.findByRole("button", { name: /Pause/ });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(ownerRequests(fetchMock)).toEqual([]);
    expect(urls(fetchMock)).toContain("/api/radar/analytics");
    expect(screen.queryByText(/Private/)).not.toBeInTheDocument();
  });
});
