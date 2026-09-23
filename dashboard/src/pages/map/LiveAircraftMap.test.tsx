import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Children, isValidElement, type ReactElement } from "react";
import LiveAircraftMap from "./LiveAircraftMap";
import { MapThemeProvider } from "./useMapTheme";
import { MLAT_HISTORY_REFRESH_MS } from "./mlatHistory";
import { ThemeProvider, useTheme } from "../../context/ThemeContext";

// Leaflet layers need a browser layout. Keep the real map controller, toolbar,
// playback and feed lifecycle; geometry and layer helpers have their own tests.
vi.mock("react-leaflet", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-leaflet")>();
  return {
    ...original,
    MapContainer: ({ children }) => (
      <div data-testid="map">
        {Children.toArray(children)
          .filter((child) => isValidElement(child) && child.type === original.Polyline && String(child.key).includes("trail-"))
          .map((child: ReactElement<{ positions: number[][] }>) => (
            <span key={child.key} data-testid="selected-trail" data-positions={JSON.stringify(child.props.positions)} />
          ))}
      </div>
    ),
  };
});

// The map reads identity from the console's AuthProvider; every case starts
// signed in.
const SIGNED_IN = { email: "owner@example.invalid", name: "Owner" };
const owner = vi.hoisted(() => ({
  user: null as { email: string; name: string } | null,
  loading: false,
  syntheticFleet: false,
}));
vi.mock("../../context/AuthContext", () => ({ useAuth: () => owner }));

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

beforeEach(() => {
  owner.user = SIGNED_IN;
  owner.syntheticFleet = false;
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
    ok: true,
    json: async () => url.endsWith("/auth/me/nodes") ? [{ node_ref: "mine" }] : {},
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

it("offers a signed-in owner no toggle where the page withholds the owner view", async () => {
  render(<MapThemeProvider><LiveAircraftMap feed="synthetic" ownerView={false} /></MapThemeProvider>);
  await screen.findByRole("button", { name: /Pause/ });
  // Long enough for the ownership answer the stub gives at once to land.
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(screen.queryByRole("checkbox", { name: "My nodes only" })).not.toBeInTheDocument();
  expect(Socket.instances.map((s) => s.url)).not.toContainEqual(expect.stringContaining("/owner"));
});

it("returns to the public feed when the owner signs out on the map", async () => {
  const { rerender } = render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  fireEvent.click(await screen.findByRole("checkbox", { name: "My nodes only" }));
  const ownerFeed = Socket.instances[Socket.instances.length - 1];
  expect(ownerFeed.url).toContain("/ws/aircraft/owner");

  owner.user = null;
  rerender(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);

  expect(screen.queryByRole("checkbox", { name: "My nodes only" })).not.toBeInTheDocument();
  expect(ownerFeed.readyState).toBe(3);
  expect(Socket.instances[Socket.instances.length - 1].url).not.toContain("/owner");

  // Signing in again offers the filter without applying it.
  owner.user = SIGNED_IN;
  rerender(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  expect(await screen.findByRole("checkbox", { name: "My nodes only" })).not.toBeChecked();
  expect(Socket.instances[Socket.instances.length - 1].url).not.toContain("/owner");
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

it("toggles pause and resume repeatedly with the space shortcut", async () => {
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  fireEvent.keyDown(window, { key: " " });
  expect(screen.getByRole("button", { name: /Resume/ })).toBeInTheDocument();
  fireEvent.keyDown(window, { key: " " });
  expect(screen.getByRole("button", { name: /Pause/ })).toBeInTheDocument();
});

it("updates the selected trail as new positions arrive without changing selection", async () => {
  window.history.replaceState(null, "", "/#hex=abc123");
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  const deliver = (lat: number, positions: number[][]) => {
    act(() => Socket.instances[0].onmessage?.({ data: JSON.stringify({
      aircraft: [{ hex: "abc123", lat, lon: -1, recent_positions: positions }],
    }) }));
  };
  const drawnPositions = () => screen.getAllByTestId("selected-trail")
    .flatMap((el) => JSON.parse(el.getAttribute("data-positions")!));

  deliver(51.001, [[51, -1, 1000, 1], [51.001, -1, 1000, 2]]);
  expect(drawnPositions()).toContainEqual([51.001, -1]);
  deliver(51.002, [[51.002, -1, 1000, 3]]);
  expect(drawnPositions()).toContainEqual([51.002, -1]);
});

it("polls the solve history only while an MLAT track is selected, and again on a fresh solve", async () => {
  // The display list is rebuilt from the animation loop, so drive its frames.
  let frames: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", vi.fn((cb: FrameRequestCallback) => frames.push(cb)));
  const runFrames = () => act(() => {
    for (let i = 0; i < 30; i++) {
      const due = frames;
      frames = [];
      due.forEach((cb) => cb(performance.now()));
    }
  });
  window.history.replaceState(null, "", "/#hex=mn1");
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  const historyCalls = () => vi.mocked(fetch).mock.calls
    .filter(([url]) => String(url).includes("/mlat-history")).map(([url]) => String(url));
  const deliver = (fields: object) => {
    act(() => Socket.instances[0].onmessage?.({ data: JSON.stringify({
      aircraft: [{ hex: "mn1", lat: 51, lon: -1, ...fields }],
    }) }));
    runFrames();
  };

  expect(historyCalls()).toEqual([]);
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    deliver({ position_source: "multinode_solve", seen: 2 });
    expect(historyCalls()).toEqual(["/api/test/mlat-history?hex=mn1"]);
    act(() => { vi.advanceTimersByTime(MLAT_HISTORY_REFRESH_MS); });
    expect(historyCalls()).toHaveLength(2);

    // `seen` falling is a new solve for this track: fetched off the schedule.
    deliver({ position_source: "multinode_solve", seen: 0.5 });
    expect(historyCalls()).toHaveLength(3);

    deliver({ position_source: "adsb" });
    act(() => { vi.advanceTimersByTime(MLAT_HISTORY_REFRESH_MS * 3); });
    expect(historyCalls()).toHaveLength(3);
  } finally {
    vi.useRealTimers();
  }
});

it("draws its live stats in the aircraft list, not floating over the map", () => {
  const { container } = render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  expect(container.querySelector(".live-map-top-right-stack")).toBeNull();
  expect(container.querySelector(".aircraft-list-panel .stats-panel")).not.toBeNull();
});

it("moves its default basemap with the console's theme", () => {
  // Console starts light, so the default basemap is Positron.
  localStorage.setItem("retina.theme", "light");
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  function Flip() {
    const { setPreference } = useTheme();
    return <button onClick={() => setPreference("dark")}>flip</button>;
  }
  render(
    <ThemeProvider>
      <Flip />
      <MapThemeProvider><LiveAircraftMap /></MapThemeProvider>
    </ThemeProvider>,
  );
  fireEvent.click(screen.getByText("flip"));
  expect(window.localStorage.getItem("retina.map.tile.theme")).toBe(JSON.stringify("voyager"));
});

function mountUnderStoredTheme(theme: string, tile: string, chosenBy: string, prefix = "retina.map.") {
  localStorage.setItem("retina.theme", theme);
  localStorage.setItem(`${prefix}tile.theme`, JSON.stringify(tile));
  localStorage.setItem(`${prefix}tile.chosenBy`, JSON.stringify(chosenBy));
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  render(
    <ThemeProvider>
      <MapThemeProvider><LiveAircraftMap /></MapThemeProvider>
    </ThemeProvider>,
  );
}

it("mounts onto its own theme's basemap when the theme that chose it has since changed", () => {
  // Dark chose Voyager; the console went light while the map was not mounted.
  mountUnderStoredTheme("light", "voyager", "dark");
  expect(window.localStorage.getItem("retina.map.tile.theme")).toBe(JSON.stringify("positron"));
});

it("keeps a hand-picked basemap when it mounts", () => {
  mountUnderStoredTheme("light", "osm", "hand");
  expect(window.localStorage.getItem("retina.map.tile.theme")).toBe(JSON.stringify("osm"));
});

it("keeps a hand-picked basemap that happens to be the other theme's default", () => {
  // Voyager is dark's default and also a stop on the cycle control; picked by
  // hand under a light console, it is the user's choice, not dark's.
  mountUnderStoredTheme("light", "voyager", "hand");
  expect(window.localStorage.getItem("retina.map.tile.theme")).toBe(JSON.stringify("voyager"));
});

it("keeps a basemap picked under the standalone map's storage keys", () => {
  mountUnderStoredTheme("light", "osm", "hand", "tf.");
  expect(window.localStorage.getItem("retina.map.tile.theme")).toBe(JSON.stringify("osm"));
  expect(window.localStorage.getItem("retina.map.tile.chosenBy")).toBe(JSON.stringify("hand"));
});

/** One simulated spawn with a transponder, as the fleet's truth snapshot carries it. */
function deliverSimTruth() {
  act(() => {
    Socket.instances[0].readyState = Socket.OPEN;
    Socket.instances[0].onopen?.();
    Socket.instances[0].onmessage?.({ data: JSON.stringify({
      aircraft: [],
      ground_truth: { abc123: [[51, -1, 3000, 1]] },
      ground_truth_meta: { abc123: { object_type: "aircraft", has_adsb: true, source: "sim" } },
    }) });
  });
  act(() => {
    for (let i = 0; i < 30; i++) {
      const calls = vi.mocked(requestAnimationFrame).mock.calls;
      calls[calls.length - 1][0](i * 16);
    }
  });
}

it("offers no ground truth where the server runs no synthetic fleet", async () => {
  render(<MapThemeProvider><LiveAircraftMap /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  deliverSimTruth();
  expect(screen.queryByRole("button", { name: "Debug Truth" })).not.toBeInTheDocument();
  expect(screen.queryByText(/^Truth:/)).not.toBeInTheDocument();
  fireEvent.keyDown(window, { key: "?" });
  expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  expect(screen.queryByText("Toggle ground-truth overlay")).not.toBeInTheDocument();
});

it("keys only the truth classes the fleet is actually sending", async () => {
  owner.syntheticFleet = true;
  render(<MapThemeProvider><LiveAircraftMap feed="synthetic" /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  expect(screen.getByRole("button", { name: "Debug Truth" })).toBeInTheDocument();
  deliverSimTruth();
  expect(screen.getByText("Truth: sim ADS-B")).toBeInTheDocument();
  expect(screen.queryByText("Truth: sim dark")).not.toBeInTheDocument();
  expect(screen.queryByText(/Truth: live/)).not.toBeInTheDocument();
});

it("offers no ground truth on the real-only feed even beside a fleet", async () => {
  owner.syntheticFleet = true;
  render(<MapThemeProvider><LiveAircraftMap feed="real" /></MapThemeProvider>);
  await screen.findByRole("checkbox", { name: "My nodes only" });
  expect(screen.queryByRole("button", { name: "Debug Truth" })).not.toBeInTheDocument();
});
