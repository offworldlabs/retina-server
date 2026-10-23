import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Children, isValidElement, type ReactNode } from "react";

import LiveAircraftMap from "./LiveAircraftMap";
import { MapThemeProvider } from "./useMapTheme";
import { archiveEntryMs } from "./archiveDisplay";
import { SOLVE_TRAIL_MAX_POINTS, TRAIL_SMOOTH_K, TRAIL_SOLVE_SIGMA_FALLBACK_M } from "./constants";
import { smoothTrailPositions } from "./trails";
import { formatHashT, parseHash } from "./useUrlHashState";
import { ADSB, GAP_HEX, KNOWN_LANE_HEX, NO_CALLSIGN_HEX, NO_SOLVE_HEX } from "./replay/__fixtures__/adsb";
import { KEYED } from "./replay/__fixtures__/scenarios";
import { DARK_EXPIRY_MS, buildReplayIndex, synthesizeFrame, type ReplayIndex } from "./replay/synthesize";
import type { AdsbResponse, ReplayAircraft, TracksResponse } from "./replay/types";
import { ARCHIVE_LAG_MS, AT_LIVE_EDGE_TITLE, SEEK_COALESCE_MS } from "./replay/useArchiveReplay";

// The archive mode of the whole map: the live map's tree with its clock moved
// onto the replay. Leaflet needs a laid-out browser, so, as in
// LiveAircraftMap.test.tsx, the map container renders none of its layers; the
// one kept is FitBounds, replaced by a probe that records what it was handed.
// The container also reads what the trails are handed, unrendered: the
// unselected-trails layer's two buffers, and the selected trail's polylines.

const fit = vi.hoisted(() => ({ props: [] as Array<{ skipInitialFit?: boolean }> }));

type LatLon = [number, number];
type Buffers = Record<string, number[][]>;
const drawn = vi.hoisted(() => ({
  trails: null as null | { frontendTrailsRef: { current: Buffers }; solveTrailsRef: { current: Buffers } },
  /** The selected trail's solid gradient segments, as the last render drew them. */
  segments: [] as LatLon[][],
  /** Its dashed continuation to the icon, or null. */
  head: null as null | LatLon[],
  /** The detection-arcs layer's props as the last render handed them, or null when it is not drawn. */
  arcs: null as null | { archived?: boolean; nowMs?: () => number },
  /** The ground-truth dots the last render handed their canvas layer, or null when it is not drawn. */
  truth: null as null | Array<{ hex: string; lat: number; lon: number; source?: string }>,
  /** The truth-comparison layers the last render mounted. */
  comparisons: [] as string[],
}));

vi.mock("./MapControls", async (importOriginal) => {
  const original = await importOriginal<typeof import("./MapControls")>();
  return {
    ...original,
    FitBounds: function FitBoundsProbe(props: { skipInitialFit?: boolean }) {
      fit.props.push(props);
      return null;
    },
  };
});

vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  type PolylineProps = { positions: LatLon[]; pathOptions?: { weight?: number; dashArray?: string } };
  return {
    ...original,
    MapContainer: ({ children }: { children: ReactNode }) => {
      drawn.segments = [];
      drawn.head = null;
      drawn.arcs = null;
      drawn.truth = null;
      drawn.comparisons = [];
      for (const child of Children.toArray(children)) {
        if (!isValidElement(child)) continue;
        const layer = (child.type as { type?: { name?: string } }).type?.name ?? "";
        if (layer === "GroundTruthCanvasLayer") {
          drawn.truth = (child.props as { aircraft: NonNullable<typeof drawn.truth> }).aircraft;
        } else if (["MatchedGroundTruthLayer", "MlatVerificationLayer", "InBeamDiagnostic"].includes(layer)) {
          drawn.comparisons.push(layer);
        }
        if ((child.type as { type?: { name?: string } }).type?.name === "AircraftTrailsLayer") {
          drawn.trails = child.props as typeof drawn.trails;
        } else if ((child.type as { type?: { name?: string } }).type?.name === "DetectionArcs") {
          drawn.arcs = child.props as typeof drawn.arcs;
        } else if (child.type === original.Polyline) {
          const { positions, pathOptions } = child.props as PolylineProps;
          if (/trail-.+-seg\d+$/.test(String(child.key))) drawn.segments.push(positions);
          else if (pathOptions?.dashArray && pathOptions.weight === 3.5) drawn.head = positions;
        }
      }
      return (
        <div data-testid="map">
          {Children.toArray(children).filter(
            (child) => isValidElement(child) && (child.type as { name?: string }).name === "FitBoundsProbe",
          )}
        </div>
      );
    },
  };
});

// A signed-out visitor: the archive explorer's acceptance case.
vi.mock("../../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, syntheticFleet: false }),
}));

// Every case renders the whole map tree, which alone takes most of the default.
vi.setConfig({ testTimeout: 20_000 });

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

/* ── The fixture: two dark tracks, one ending mid-window ─────────────── */

const MIN = 60_000;
const SPAN_MS = 15 * MIN;
const DARK = KEYED.dark;
const INDEX = buildReplayIndex(DARK);
const frameAt = (ms: number) => synthesizeFrame(INDEX, ms);
const ceilSecond = (ms: number) => Math.ceil(ms / 1000) * 1000;
const floorSecond = (ms: number) => Math.floor(ms / 1000) * 1000;

// Read off the rows rather than written down, so a real cut of
// /api/data/tracks can replace the file (see scenarios.ts).
const TRACKS = Object.values(
  DARK.rows.reduce<Record<string, { first: number; last: number }>>((acc, r) => {
    const key = r.track_key ?? "";
    const published = Math.max(r.solve_ts_ms, r.frame_ts_ms);
    const t = (acc[key] ??= { first: published, last: r.frame_ts_ms });
    t.first = Math.min(t.first, published);
    t.last = Math.max(t.last, r.frame_ts_ms);
    return acc;
  }, {}),
);
const ENDING = TRACKS.reduce((a, b) => (b.last < a.last ? b : a));
const LATEST_START = Math.max(...TRACKS.map((t) => t.first));
const EARLIEST_START = Math.min(...TRACKS.map((t) => t.first));
/** Both tracks on the map, each freshly solved. */
const T_BOTH = floorSecond((LATEST_START + ENDING.last) / 2);
/** The first track has expired; the other carries on. */
const T_ONE = ceilSecond(ENDING.last + DARK_EXPIRY_MS + 1);
/** Before the later track's first solve: only the earlier one exists. */
const T_EARLY = ceilSecond(EARLIEST_START + 1);

/* ── Stubs ────────────────────────────────────────────────────────────── */

type Answer = { ok: boolean; status: number; json: () => Promise<unknown> };
const ok = (body: unknown): Answer => ({ ok: true, status: 200, json: async () => body });

const AVAILABILITY = {
  from: "2026-10-15",
  to: "2026-10-21",
  fleet: "real",
  hours: [{ start_ms: floorSecond(T_BOTH / 3_600_000) * 3_600_000, count: DARK.count, unclassified: 0 }],
  total: DARK.count,
};

/** What /api/data/tracks answers: DARK unless a case says otherwise. */
let tracksBody: TracksResponse = DARK;
/** What /api/data/adsb answers: no aircraft unless a case says otherwise. */
let adsbBody: AdsbResponse | null = null;
const fetchMock = vi.fn(async (url: string, _init?: RequestInit): Promise<Answer> => {
  if (url.startsWith("/api/data/tracks/availability")) return ok(AVAILABILITY);
  if (url.startsWith("/api/data/tracks?")) return ok(tracksBody);
  if (url.startsWith("/api/data/detections?")) {
    const query = new URL(url, "https://app.retina.fm").searchParams;
    const [from, to] = [Number(query.get("from")), Number(query.get("to"))];
    return ok({ from, to, fleet: "real", count: 0, truncated: false, local: true, rows: [] });
  }
  if (url.startsWith("/api/data/adsb?")) {
    const query = new URL(url, "https://app.retina.fm").searchParams;
    const [from, to] = [Number(query.get("from")), Number(query.get("to"))];
    return ok(adsbBody ?? { from, to, fleet: "real", count: 0, truncated: false, rows: [] });
  }
  return ok({});
});

/** Every /api/data/adsb request, as query parameters. */
function adsbRequests(): URLSearchParams[] {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).startsWith("/api/data/adsb?"))
    .map(([url]) => new URL(String(url), "https://app.retina.fm").searchParams);
}

/** Every /api/data/detections request, as query parameters. */
function detectionsRequests(): URLSearchParams[] {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).startsWith("/api/data/detections?"))
    .map(([url]) => new URL(String(url), "https://app.retina.fm").searchParams);
}

/** Every /api/data/tracks window request, as [query, init]. */
function tracksRequests(): Array<[URLSearchParams, RequestInit]> {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).startsWith("/api/data/tracks?"))
    .map(([url, init]) => [new URL(String(url), "https://app.retina.fm").searchParams, init ?? {}]);
}

// The animation loop builds the React display list once every 30 frames.
let frames: FrameRequestCallback[] = [];
let frameTs = 0;
function runFrames(n = 31, stepMs = 16) {
  act(() => {
    for (let i = 0; i < n; i++) {
      const due = frames;
      frames = [];
      frameTs += stepMs;
      due.forEach((cb) => cb(frameTs));
    }
  });
}

/** Lets timers and answers land, inside act so React takes them in. */
const settle = (ms = 0) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

async function until(check: () => boolean, what: string) {
  for (let i = 0; i < 150; i++) {
    if (check()) return;
    await settle(20);
  }
  throw new Error(`timed out waiting for ${what}`);
}

beforeEach(() => {
  window.history.replaceState(null, "", "/map");
  Socket.instances = [];
  frames = [];
  fit.props = [];
  drawn.trails = null;
  drawn.segments = [];
  drawn.head = null;
  drawn.arcs = null;
  drawn.truth = null;
  drawn.comparisons = [];
  tracksBody = DARK;
  adsbBody = null;
  fetchMock.mockClear();
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("requestAnimationFrame", vi.fn((cb: FrameRequestCallback) => frames.push(cb)));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

/* ── Page helpers ─────────────────────────────────────────────────────── */

const LINK_VIEW = "lat=34.85&lon=-82.39&z=10";

function showMap(hash: string, feed?: "real" | "synthetic") {
  window.history.replaceState(null, "", `/map${hash}`);
  return render(<MapThemeProvider><LiveAircraftMap feed={feed} /></MapThemeProvider>);
}

const pill = () => document.querySelector(".connection-badge") as HTMLElement;
const bar = () => screen.queryByRole("region", { name: "Archive replay" });
const aircraftCount = () => document.querySelector(".aircraft-count")?.textContent;
const scrubber = () => screen.getByRole("slider", { name: "Replay position" });
const statsPanel = () => screen.getByRole("region", { name: "Replay stats" });
const latitude = () => {
  const label = Array.from(document.querySelectorAll(".detail-panel .detail-label"))
    .find((el) => el.textContent === "Latitude");
  return label?.nextElementSibling?.textContent;
};

/** Opens the map on an archive link and waits for its first replayed frame. */
async function openArchiveAt(t: number, extra = "", index: ReplayIndex = INDEX) {
  const view = showMap(`#${LINK_VIEW}&t=${formatHashT(t)}${extra}`);
  const summary = `${index.trackCount} track${index.trackCount === 1 ? "" : "s"}`;
  await until(() => Boolean(bar()?.textContent?.includes(summary)), "the window to load");
  await settle();
  runFrames();
  return view;
}

/** Seeks with the scrubber, past the replay's seek coalescing, and redraws. */
async function seekTo(ms: number) {
  await settle(SEEK_COALESCE_MS + 20);
  fireEvent.change(scrubber(), { target: { value: String(ms) } });
  await settle();
  runFrames();
}

describe("the fixture", () => {
  it("has both tracks at T_BOTH, the later one alone at T_ONE, and the earlier alone at T_EARLY", () => {
    expect(INDEX.trackCount).toBe(2);
    expect(frameAt(T_BOTH).aircraft).toHaveLength(2);
    // Freshly solved: under 5 s since the solve, about one dark solve interval (3.5 to 4 s in this cut).
    expect(frameAt(T_BOTH).aircraft.every((ac) => (ac.seen ?? Infinity) < 5)).toBe(true);
    expect(frameAt(T_ONE).aircraft).toHaveLength(1);
    expect(frameAt(T_EARLY).aircraft).toHaveLength(1);
    expect(T_ONE - T_BOTH).toBeGreaterThan(10_000);
    expect(T_BOTH - T_EARLY).toBeGreaterThan(10_000);
  });
});

describe("an archive deep link", () => {
  it("replays the window around t with no socket, under the ARCHIVE pill and the archive bar", async () => {
    await openArchiveAt(T_BOTH);

    expect(Socket.instances).toHaveLength(0);
    expect(pill()).toHaveTextContent(/^ARCHIVE$/);
    expect(bar()).toBeInTheDocument();
    expect(document.querySelector(".playback-bar")).toBeNull();

    const requests = tracksRequests();
    expect(requests).toHaveLength(1);
    const [query, init] = requests[0];
    expect([...query]).toEqual([
      ["from", String(T_BOTH - SPAN_MS)],
      ["to", String(T_BOTH + SPAN_MS)],
      ["fleet", "real"],
    ]);
    expect(init.referrerPolicy).toBe("strict-origin");

    expect(aircraftCount()).toBe("2 aircraft");
    expect(document.querySelector(".al-count")?.textContent).toBe("2");
    // Still no socket once the replay has run.
    expect(Socket.instances).toHaveLength(0);
  });

  it("ages replayed solves on the replay clock, which a paused replay holds still", async () => {
    await openArchiveAt(T_BOTH);
    expect(aircraftCount()).toBe("2 aircraft");
    const stats = within(statsPanel());
    expect(stats.getByText("MLAT dark").nextElementSibling).toHaveTextContent(/^2$/);
    // Solved a second or two before the replay instant, and hours before now.
    expect(statsPanel()).not.toHaveTextContent(/stale|hidden/);

    // An hour of wall time on a paused replay: on the wall clock both solves
    // would be an hour old, past every drift and age budget, and drop.
    const wall = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => wall + 60 * MIN);
    runFrames();
    expect(aircraftCount()).toBe("2 aircraft");
    expect(stats.getByText("MLAT dark").nextElementSibling).toHaveTextContent(/^2$/);
    expect(statsPanel()).not.toHaveTextContent(/stale|hidden/);
  });

  it("draws each icon at its solve, without dead reckoning or a glide between solves", async () => {
    const tracks = frameAt(T_BOTH).aircraft;
    const lasting = tracks.find((ac) => frameAt(T_ONE).aircraft.some((b) => b.hex === ac.hex))!;
    await openArchiveAt(T_BOTH, `&hex=${lasting.hex}`);
    expect(latitude()).toBe(lasting.lat.toFixed(5));

    // A few seconds on, within the replay's reset gap, so the stores carry
    // over and the live loop would glide from the last fix to the new one.
    const next = T_BOTH + 5_000;
    const moved = frameAt(next).aircraft.find((ac) => ac.hex === lasting.hex)!;
    expect(moved.lat.toFixed(5)).not.toBe(lasting.lat.toFixed(5));
    await seekTo(next);
    expect(latitude()).toBe(moved.lat.toFixed(5));
    // And it stays there while the replay holds the instant.
    runFrames(61, 50);
    expect(latitude()).toBe(moved.lat.toFixed(5));
  });

  it("keeps nothing from another instant across a seek, in either direction", async () => {
    await openArchiveAt(T_BOTH);
    expect(aircraftCount()).toBe("2 aircraft");

    await seekTo(T_ONE);
    expect(aircraftCount()).toBe("1 aircraft");

    await seekTo(T_BOTH);
    expect(aircraftCount()).toBe("2 aircraft");

    // Back before the second track's first solve: its fix from the later
    // instant must not survive into this one.
    await seekTo(T_EARLY);
    expect(aircraftCount()).toBe("1 aircraft");
  });

  it("plays and pauses the replay on Space and leaves the live pause alone", async () => {
    await openArchiveAt(T_BOTH);
    const controls = within(bar()!);
    expect(controls.getByRole("button", { name: "Play" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: " " });
    expect(controls.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause replay" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: " " });
    expect(controls.getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play replay" })).toBeInTheDocument();

    // The live pause would have frozen the feed and the animation loop, and
    // put up the live playback bar.
    expect(document.querySelector(".playback-bar")).toBeNull();
    await seekTo(T_ONE);
    expect(aircraftCount()).toBe("1 aircraft");
  });

  it("returns to the live feed on Jump to now", async () => {
    await openArchiveAt(T_BOTH);
    expect(Socket.instances).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Jump to now" }));

    expect(parseHash().t).toBeUndefined();
    expect(window.location.hash).not.toMatch(/(^#|&)(t|span)=/);
    expect(window.location.hash).toContain(LINK_VIEW.split("&")[0]);
    expect(bar()).toBeNull();
    expect(Socket.instances).toHaveLength(1);
    expect(Socket.instances[0].url).toMatch(/\/ws\/aircraft/);

    act(() => {
      Socket.instances[0].readyState = Socket.OPEN;
      Socket.instances[0].onopen?.();
    });
    expect(pill()).toHaveTextContent(/^LIVE$/);
    expect(screen.getByRole("button", { name: "⏸ Pause" })).toBeInTheDocument();
  });

  it("loads the window around a new t when the hash changes", async () => {
    await openArchiveAt(T_BOTH);
    expect(tracksRequests()).toHaveLength(1);

    const later = T_BOTH - 3 * 60 * MIN;
    await act(async () => {
      window.location.hash = `#${LINK_VIEW}&t=${formatHashT(later)}`;
    });
    await until(() => tracksRequests().length === 2, "the new window's request");

    const [query] = tracksRequests()[1];
    expect(query.get("from")).toBe(String(later - SPAN_MS));
    expect(query.get("to")).toBe(String(later + SPAN_MS));
    expect(query.get("fleet")).toBe("real");
    expect(pill()).toHaveTextContent(/^ARCHIVE$/);
    expect(Socket.instances).toHaveLength(0);
  });
});

describe("the detection arcs in replay", () => {
  it("loads the real fleet's detections around the clock when the arcs layer is on, and draws them archived on the replay clock", async () => {
    await openArchiveAt(T_BOTH, "&layers=d");
    await until(() => detectionsRequests().length > 0, "the detections request");

    const query = detectionsRequests()[0];
    expect(query.get("fleet")).toBe("real");
    expect(Number(query.get("from"))).toBeLessThanOrEqual(T_BOTH);
    expect(Number(query.get("to"))).toBeGreaterThanOrEqual(T_BOTH);

    expect(drawn.arcs).not.toBeNull();
    expect(drawn.arcs!.archived).toBe(true);
    // The replay clock, whatever the wall clock says.
    vi.spyOn(Date, "now").mockReturnValue(T_BOTH + 60 * MIN);
    expect(drawn.arcs!.nowMs!()).toBe(T_BOTH);
  });

  it("asks for no detections when the arcs layer is off", async () => {
    await openArchiveAt(T_BOTH, "&layers=t");
    await settle(50);

    expect(detectionsRequests()).toHaveLength(0);
    expect(drawn.arcs).toBeNull();
  });
});

describe("ADS-B traffic in replay", () => {
  const at = (h: number, m: number, s = 0) => Date.UTC(2026, 9, 18, h, m, s);
  /** adbeaa's last snapshot before it drops out is 19:59:10Z, 15 s before this. */
  const T_ALL = at(19, 59, 25);
  const truthHexes = () => (drawn.truth ?? []).map((ac) => ac.hex).sort();

  /** Opens the real-fleet map, as deployed, on an archive link, and waits for its first replayed frame. */
  async function openRealArchiveAt(t: number, layers: string) {
    showMap(`#${LINK_VIEW}&t=${formatHashT(t)}&layers=${layers}`, "real");
    await until(() => Boolean(bar()?.textContent?.includes("2 tracks")), "the window to load");
    await settle();
    runFrames();
  }

  async function untilTruth(what: string) {
    await until(() => {
      runFrames();
      return (drawn.truth?.length ?? 0) > 0;
    }, what);
  }

  it("shows on G the real fleet's archived traffic of the replayed instant, as ADS-B traffic, and asks for none before", async () => {
    adsbBody = ADSB.traffic;
    await openRealArchiveAt(T_ALL, "t");
    await settle(50);
    expect(adsbRequests()).toHaveLength(0);
    expect(drawn.truth).toBeNull();

    fireEvent.keyDown(window, { key: "g" });
    await untilTruth("the traffic");
    const query = adsbRequests()[0];
    expect(query.get("fleet")).toBe("real");
    expect(query.get("bbox")).toBeNull();
    expect(Number(query.get("to")) - Number(query.get("from"))).toBeLessThanOrEqual(60 * MIN);
    expect(Number(query.get("from"))).toBeLessThanOrEqual(T_ALL);
    expect(Number(query.get("to"))).toBeGreaterThan(T_ALL);

    expect(truthHexes()).toEqual([NO_SOLVE_HEX, KNOWN_LANE_HEX, GAP_HEX, NO_CALLSIGN_HEX].sort());
    const noSolve = drawn.truth!.find((ac) => ac.hex === NO_SOLVE_HEX)!;
    // a07caf's snapshot at 19:59:20Z, the latest at or before the clock.
    expect([noSolve.lat, noSolve.lon]).toEqual([34.92839, -82.129249]);
    expect(document.querySelector(".map-legend")).toHaveTextContent("ADS-B traffic");
    expect(screen.getByRole("button", { name: "ADS-B traffic" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.keyDown(window, { key: "?" });
    expect(screen.getByText("G").closest("tr")).toHaveTextContent("Toggle archived ADS-B traffic");
    fireEvent.keyDown(window, { key: "Escape" });

    fireEvent.keyDown(window, { key: "g" });
    runFrames();
    expect(drawn.truth).toBeNull();
    expect(adsbRequests()).toHaveLength(1);
  });

  it("draws no solve-versus-truth comparison and asks for no live verification in archive mode", async () => {
    adsbBody = ADSB.traffic;
    await openRealArchiveAt(T_ALL, "gbt");
    await untilTruth("the traffic");
    expect(drawn.comparisons).toEqual([]);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("mlat"))).toEqual([]);
  });

  it("ages and prunes the traffic on the replay clock: kept while paused, gone with the frame that leaves it out", async () => {
    adsbBody = ADSB.traffic;
    await openRealArchiveAt(T_ALL, "gt");
    await untilTruth("the traffic");
    // Days behind the wall clock, held still: nothing ages out.
    expect(Date.now() - T_ALL).toBeGreaterThan(24 * 60 * MIN);
    await settle(100);
    runFrames(62);
    expect(truthHexes()).toContain(GAP_HEX);

    // 6 s on, no reset: adbeaa is 21 s past its last snapshot, the others fresh.
    await seekTo(T_ALL + 6_000);
    runFrames();
    expect(truthHexes()).toEqual([NO_SOLVE_HEX, KNOWN_LANE_HEX, NO_CALLSIGN_HEX].sort());
  });
});

describe("a link past the live edge (L1, L2)", () => {
  /** The review's repro, on a wall clock the test holds: now 15:23:18Z, a link two hours ahead. */
  const WALL = Date.UTC(2026, 9, 20, 15, 23, 18);
  const AHEAD = WALL + 2 * 60 * MIN;
  /** The live edge at WALL, 15:21:18Z: where the replay opens instead. */
  const EDGE = WALL - ARCHIVE_LAG_MS;
  const CLAMPED = "The link's time is past the latest archived moment; showing 15:21:18 UTC.";

  let wall = WALL;
  beforeEach(() => {
    wall = WALL;
    vi.spyOn(Date, "now").mockImplementation(() => wall);
  });

  /** The two play buttons, the bar's and the toolbar's. */
  const plays = () => [within(bar()!).getByRole("button", { name: "Play" }), screen.getByRole("button", { name: "Play replay" })];

  it("names the time shown in the URL at once, says why, and leaves Play off until a step back", async () => {
    const entries = window.history.length;
    showMap(`#${LINK_VIEW}&t=${formatHashT(AHEAD)}`);
    // Before the window lands, in place, and with no history entry of its own.
    expect(parseHash().t).toBe(EDGE);
    expect(window.location.hash).toBe(`#${LINK_VIEW}&t=${formatHashT(EDGE)}`);
    expect(window.history.length).toBe(entries);

    await until(() => Boolean(bar()?.textContent?.includes(CLAMPED)), "the notice");
    const controls = within(bar()!);
    expect(controls.getByRole("status")).toHaveTextContent(CLAMPED);
    for (const play of plays()) {
      expect(play).toBeDisabled();
      expect(play).toHaveAttribute("title", AT_LIVE_EDGE_TITLE);
    }

    // Space, the map's play/pause in the archive, does nothing and throws nothing.
    const thrown: unknown[] = [];
    const onError = (e: ErrorEvent) => thrown.push(e.error);
    window.addEventListener("error", onError);
    try {
      fireEvent.keyDown(window, { key: " " });
      await settle();
    } finally {
      window.removeEventListener("error", onError);
    }
    expect(thrown).toEqual([]);
    expect(plays().every((play) => play.hasAttribute("disabled"))).toBe(true);
    expect(controls.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(controls.getByRole("status")).toHaveTextContent(CLAMPED);

    // A step back a moment later: off the edge, so Play is back and the notice gone.
    wall += 100;
    fireEvent.click(controls.getByRole("button", { name: "Step back 1 second" }));
    await settle();
    expect(parseHash().t).toBe(EDGE - 1_000);
    expect(controls.getByRole("status")).toHaveTextContent(/^2 tracks · /);
    for (const play of plays()) {
      expect(play).toBeEnabled();
      expect(play).not.toHaveAttribute("title", AT_LIVE_EDGE_TITLE);
    }
    expect(tracksRequests()).toHaveLength(1);
  });

  it("does the same when the hash changes to such a time", async () => {
    await openArchiveAt(T_BOTH);
    expect(plays().every((play) => !play.hasAttribute("disabled"))).toBe(true);

    await act(async () => {
      window.location.hash = `#${LINK_VIEW}&t=${formatHashT(AHEAD)}`;
    });
    await until(() => Boolean(bar()?.textContent?.includes(CLAMPED)), "the notice");
    expect(parseHash().t).toBe(EDGE);
    expect(window.location.hash).not.toContain(formatHashT(AHEAD));
    const [query] = tracksRequests()[1];
    expect(query.get("from")).toBe(String(EDGE - SPAN_MS));
    for (const play of plays()) expect(play).toBeDisabled();
  });
});

describe("where there is no archive", () => {
  it("stays live on /sim whatever the hash says, and offers no archive", async () => {
    showMap(`#${LINK_VIEW}&t=${formatHashT(T_BOTH)}`, "synthetic");
    await settle(50);

    expect(Socket.instances).toHaveLength(1);
    expect(tracksRequests()).toHaveLength(0);
    expect(bar()).toBeNull();
    expect(pill()).not.toHaveTextContent("ARCHIVE");

    fireEvent.click(screen.getByTitle("More actions"));
    expect(screen.getAllByRole("menuitem").length).toBeGreaterThan(0);
    expect(screen.queryByRole("menuitem", { name: /Open archive/ })).toBeNull();
  });
});

describe("Open archive", () => {
  it("opens the archive an hour back, on the minute, from the live map", async () => {
    showMap(`#${LINK_VIEW}`);
    await settle();
    expect(Socket.instances).toHaveLength(1);
    expect(bar()).toBeNull();

    fireEvent.click(screen.getByTitle("More actions"));
    const before = Date.now();
    fireEvent.click(screen.getByRole("menuitem", { name: /Open archive/ }));
    const after = Date.now();

    const t = parseHash().t!;
    expect([archiveEntryMs(before), archiveEntryMs(after)]).toContain(t);
    expect(window.location.hash).toContain(`t=${formatHashT(t)}`);
    expect(pill()).toHaveTextContent(/^ARCHIVE$/);
    expect(bar()).toBeInTheDocument();
    // The live socket is closed and no other opens.
    expect(Socket.instances).toHaveLength(1);
    expect(Socket.instances[0].readyState).toBe(3);

    await until(() => tracksRequests().length === 1, "the window's request");
    const [query] = tracksRequests()[0];
    expect(query.get("from")).toBe(String(t - SPAN_MS));
    expect(query.get("to")).toBe(String(t + SPAN_MS));
  });
});

describe("the first-data fit", () => {
  const skipped = () => fit.props.map((p) => p.skipInitialFit);

  it("gives way to the view an archive link names", async () => {
    showMap(`#${LINK_VIEW}&t=${formatHashT(T_BOTH)}`);
    await settle();
    expect(skipped().length).toBeGreaterThan(0);
    expect(new Set(skipped())).toEqual(new Set([true]));
  });

  it("still frames the data for a live link", async () => {
    showMap(`#${LINK_VIEW}`);
    await settle();
    expect(skipped().length).toBeGreaterThan(0);
    expect(new Set(skipped())).toEqual(new Set([false]));
  });

  it("still frames the data for an archive link with no centre", async () => {
    showMap(`#t=${formatHashT(T_BOTH)}`);
    await settle();
    expect(bar()).toBeInTheDocument();
    expect(skipped().length).toBeGreaterThan(0);
    expect(new Set(skipped())).toEqual(new Set([false]));
  });
});

/* ── Trails ───────────────────────────────────────────────────────────── */

// A dark track and a known (mn-adsb) one alive together, each with more
// archived trail than the map's buffer for its lane keeps.
const KNOWN = KEYED.known;
const KNOWN_INDEX = buildReplayIndex(KNOWN);
const knownFrameAt = (ms: number) => synthesizeFrame(KNOWN_INDEX, ms);
/** Ten seconds before the first of the two tracks' last solves. */
const T_LONG = floorSecond(
  Math.min(
    ...Object.values(
      KNOWN.rows.reduce<Record<string, number>>((acc, r) => {
        const key = r.track_key ?? "";
        acc[key] = Math.max(acc[key] ?? -Infinity, r.frame_ts_ms);
        return acc;
      }, {}),
    ),
  ) - 10_000,
);
/** The frontend buffer's depth (LiveAircraftMap.tsx FRONTEND_TRAIL_MAX_POINTS). */
const FRONTEND_TRAIL_MAX_POINTS = 60;

/** Unmounts the map mid-case, with the animation frames its loop still had queued. */
function unmount() {
  cleanup();
  frames = [];
}

/** A replayed entry's trail as its lane's buffer holds it: the newest points as [lat, lon, ts_s], the entry's sigma on the last. */
function laneBuffer(ac: ReplayAircraft): { lane: "solve" | "frontend"; points: number[][] } {
  const dark = !ac.adsb_assisted;
  const kept = ac.recent_positions!.slice(-(dark ? SOLVE_TRAIL_MAX_POINTS : FRONTEND_TRAIL_MAX_POINTS));
  const points = kept.map(([lat, lon, , ts]) => [lat, lon, ts]);
  if (dark && typeof ac.pos_sigma_m === "number" && ac.pos_sigma_m > 0) points[points.length - 1].push(ac.pos_sigma_m);
  return { lane: dark ? "solve" : "frontend", points };
}

const buffers = () => ({ solve: drawn.trails!.solveTrailsRef.current, frontend: drawn.trails!.frontendTrailsRef.current });

/** Every buffer holds its track's trail in the frame at `ms`, and no other track has one. */
function expectBuffersOf(frame: { aircraft: ReplayAircraft[] }) {
  const { solve, frontend } = buffers();
  const want: Record<"solve" | "frontend", Buffers> = { solve: {}, frontend: {} };
  for (const ac of frame.aircraft) {
    const { lane, points } = laneBuffer(ac);
    want[lane][ac.hex] = points;
  }
  expect(solve).toEqual(want.solve);
  expect(frontend).toEqual(want.frontend);
}

/** The selected trail's solid body: its gradient segments joined (each starts on the last one's end). */
const selectedBody = (): LatLon[] => drawn.segments.flatMap((seg, i) => (i ? seg.slice(1) : seg));

/** The selected dark trail a frame's entry should draw: its solve trail averaged, then dashed to the icon. */
function darkSelection(ac: ReplayAircraft) {
  const { smoothed, head } = smoothTrailPositions(laneBuffer(ac).points as [number, number, number, number?][], {
    k: TRAIL_SMOOTH_K,
    sigmaFallbackM: TRAIL_SOLVE_SIGMA_FALLBACK_M,
  });
  return { body: smoothed, head: [smoothed[smoothed.length - 1], ...head, [ac.lat, ac.lon]] };
}

describe("trails in replay", () => {
  it("has a dark track and a known one at T_LONG, each with more trail than its lane's buffer keeps", () => {
    const tracks = knownFrameAt(T_LONG).aircraft;
    expect(tracks).toHaveLength(2);
    const dark = tracks.find((ac) => !ac.adsb_assisted)!;
    const known = tracks.find((ac) => ac.adsb_assisted)!;
    expect(dark.recent_positions!.length).toBeGreaterThan(SOLVE_TRAIL_MAX_POINTS);
    expect(known.recent_positions!.length).toBeGreaterThan(FRONTEND_TRAIL_MAX_POINTS);
    expect(dark.pos_sigma_m).toBeGreaterThan(0);
    expect(knownFrameAt(T_LONG + 8_000).aircraft.map((ac) => ac.hex).sort()).toEqual(tracks.map((ac) => ac.hex).sort());
    expect(knownFrameAt(T_LONG - 40_000).aircraft.map((ac) => ac.hex).sort()).toEqual(tracks.map((ac) => ac.hex).sort());
  });

  it("holds every track's archived trail at a paused deep link, in its lane's buffer", async () => {
    tracksBody = KNOWN;
    await openArchiveAt(T_LONG, "", KNOWN_INDEX);
    const frame = knownFrameAt(T_LONG);
    expectBuffersOf(frame);
    // Drawable: the trails layer needs two points.
    const { solve, frontend } = buffers();
    const dark = frame.aircraft.find((ac) => !ac.adsb_assisted)!;
    const known = frame.aircraft.find((ac) => ac.adsb_assisted)!;
    expect(solve[dark.hex]).toHaveLength(SOLVE_TRAIL_MAX_POINTS);
    expect(frontend[known.hex]).toHaveLength(FRONTEND_TRAIL_MAX_POINTS);
    // The sigma rides on the newest solve alone; the archive has none for the rest.
    const [newestLat, newestLon, , newestTs] = dark.recent_positions![dark.recent_positions!.length - 1];
    expect(solve[dark.hex][SOLVE_TRAIL_MAX_POINTS - 1]).toEqual([newestLat, newestLon, newestTs, dark.pos_sigma_m]);
    expect(solve[dark.hex].slice(0, -1).every((p) => p.length === 3)).toBe(true);
    // Nothing past the clock.
    expect(Math.max(...solve[dark.hex].map((p) => p[2]), ...frontend[known.hex].map((p) => p[2]))).toBeLessThanOrEqual(T_LONG / 1000);

    // On DARK too, both tracks dark: each one's buffer is drawable at once.
    unmount();
    tracksBody = DARK;
    await openArchiveAt(T_BOTH);
    const both = frameAt(T_BOTH);
    expect(both.aircraft.every((ac) => ac.recent_positions!.length >= 2)).toBe(true);
    expectBuffersOf(both);
    expect(both.aircraft.every((ac) => buffers().solve[ac.hex].length >= 2)).toBe(true);
  });

  it("refills them from the new frame's trails after a seek more than 10 s on, and after a seek back", async () => {
    tracksBody = KNOWN;
    await openArchiveAt(T_LONG - 30_000, "", KNOWN_INDEX);
    expectBuffersOf(knownFrameAt(T_LONG - 30_000));

    await seekTo(T_LONG);
    expectBuffersOf(knownFrameAt(T_LONG));
    expect(Object.values(buffers().solve)[0]).toHaveLength(SOLVE_TRAIL_MAX_POINTS);

    await seekTo(T_LONG - 40_000);
    expectBuffersOf(knownFrameAt(T_LONG - 40_000));

    // Across instants where a track comes and goes: none keeps a buffer from
    // an instant it is not in.
    unmount();
    tracksBody = DARK;
    await openArchiveAt(T_BOTH);
    expectBuffersOf(frameAt(T_BOTH));
    await seekTo(T_ONE);
    expectBuffersOf(frameAt(T_ONE));
    expect(Object.keys(buffers().solve)).toHaveLength(1);
    await seekTo(T_EARLY);
    expectBuffersOf(frameAt(T_EARLY));
    expect(Object.keys(buffers().solve)).toHaveLength(1);
  });

  it("keeps the buffer of a track whose row has not moved and rebuilds the one whose row has, stepping on 1 s at a time", async () => {
    tracksBody = KNOWN;
    await openArchiveAt(T_LONG, "", KNOWN_INDEX);
    const requests = tracksRequests().length;
    const held = () => ({ ...buffers().solve, ...buffers().frontend });
    let prevFrame = knownFrameAt(T_LONG);
    let prev = held();
    const kept: Record<string, number> = {};
    const rebuilt: Record<string, number> = {};
    for (let s = 1; s <= 8; s++) {
      const t = T_LONG + s * 1000;
      await seekTo(t);
      const frame = knownFrameAt(t);
      expectBuffersOf(frame);
      const now = held();
      for (const ac of frame.aircraft) {
        const was = prevFrame.aircraft.find((p) => p.hex === ac.hex)!;
        if (was.frame_ts_ms === ac.frame_ts_ms && was.solve_ts_ms === ac.solve_ts_ms) {
          expect(now[ac.hex], `${ac.hex} at +${s} s`).toBe(prev[ac.hex]);
          kept[ac.hex] = (kept[ac.hex] ?? 0) + 1;
        } else {
          expect(now[ac.hex], `${ac.hex} at +${s} s`).not.toBe(prev[ac.hex]);
          rebuilt[ac.hex] = (rebuilt[ac.hex] ?? 0) + 1;
        }
      }
      prevFrame = frame;
      prev = now;
    }
    // Both happened to each track, the dark lane's and the known one's, all from one loaded window.
    const hexes = prevFrame.aircraft.map((ac) => ac.hex).sort();
    expect(hexes).toHaveLength(2);
    expect(Object.keys(kept).sort()).toEqual(hexes);
    expect(Object.keys(rebuilt).sort()).toEqual(hexes);
    expect(tracksRequests()).toHaveLength(requests);
  });

  it("rebuilds every buffer after a reset, though the frame hands back the trail a buffer was built from", async () => {
    tracksBody = KNOWN;
    const dark = knownFrameAt(T_LONG).aircraft.find((ac) => !ac.adsb_assisted)!;
    await openArchiveAt(T_LONG, "", KNOWN_INDEX);
    const requests = tracksRequests().length;
    const first = buffers().solve[dark.hex];
    expect(first).toHaveLength(SOLVE_TRAIL_MAX_POINTS);

    // Back to before the dark track's first solve (a reset), where the frame
    // leaves it out, then on to T_LONG again (another): the index in use
    // hands the dark track the very trail its first buffer was built from.
    const before = floorSecond(Math.min(...KNOWN.rows.filter((r) => r.lane === "dark").map((r) => r.frame_ts_ms)) - 5_000);
    await seekTo(before);
    expect(buffers().solve[dark.hex]).toBeUndefined();
    await seekTo(T_LONG);
    expect(tracksRequests()).toHaveLength(requests);
    expectBuffersOf(knownFrameAt(T_LONG));
    expect(buffers().solve[dark.hex]).not.toBe(first);
    expect(buffers().solve[dark.hex]).toEqual(first);
  });

  it("never draws less of a selected track's trail than the deep link drew, stepping on 1 s at a time", async () => {
    tracksBody = KNOWN;
    for (const assisted of [false, true]) {
      const hex = knownFrameAt(T_LONG).aircraft.find((ac) => ac.adsb_assisted === assisted)!.hex;
      await openArchiveAt(T_LONG, `&hex=${hex}`, KNOWN_INDEX);
      const atLink = selectedBody().length;
      expect(atLink).toBeGreaterThanOrEqual(2);
      const drawnPoints = [atLink];
      for (let s = 1; s <= 8; s++) {
        await seekTo(T_LONG + s * 1000);
        drawnPoints.push(selectedBody().length);
      }
      expect(Math.min(...drawnPoints), `${assisted ? "known" : "dark"} track: ${drawnPoints.join(", ")}`).toBe(atLink);
      unmount();
    }
  });

  it("draws a selected dark track from the frame shown, at the link and after a seek back, and not at all where it is not", async () => {
    tracksBody = KNOWN;
    const dark = knownFrameAt(T_LONG).aircraft.find((ac) => !ac.adsb_assisted)!;
    await openArchiveAt(T_LONG, `&hex=${dark.hex}`, KNOWN_INDEX);
    let want = darkSelection(dark);
    expect(selectedBody()).toEqual(want.body);
    expect(drawn.head).toEqual(want.head);

    // Back 40 s, paused: the trail and its dashed head end where the solve
    // was then, not where it was at the instant left.
    const earlier = knownFrameAt(T_LONG - 40_000).aircraft.find((ac) => ac.hex === dark.hex)!;
    await seekTo(T_LONG - 40_000);
    runFrames(61, 50);
    want = darkSelection(earlier);
    expect(selectedBody()).toEqual(want.body);
    expect(drawn.head).toEqual(want.head);

    // Before its first solve the track is not in the frame, and neither is its trail.
    const before = floorSecond(Math.min(...KNOWN.rows.filter((r) => r.lane === "dark").map((r) => r.frame_ts_ms)) - 5_000);
    expect(knownFrameAt(before).aircraft.some((ac) => ac.hex === dark.hex)).toBe(false);
    await seekTo(before);
    runFrames(61, 50);
    expect(drawn.segments).toEqual([]);
    expect(drawn.head).toBeNull();
  });
});
