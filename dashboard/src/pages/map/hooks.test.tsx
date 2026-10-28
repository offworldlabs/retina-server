import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onSessionEnded } from "../../api/client";
import { useAircraftFeed, useMapAuth, useNodes } from "./hooks";

vi.mock("../../utils/domains", () => ({ usesRealOnlyFeed: false }));

// The map reads identity from the console's AuthProvider.
const auth = vi.hoisted(() => ({ current: { user: null as unknown, loading: false } }));
vi.mock("../../context/AuthContext", () => ({ useAuth: () => auth.current }));

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
  /** The server closing the socket with `code`, as the browser reports it. */
  closedBy(code: number) { this.readyState = 3; this.onclose?.({ code }); }
  message(data: object) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

const aircraft = (hex: string, lat = 51) => ({ hex, lat, lon: -1, node_ref: "node-a" });
const response = (hex: string) => ({ ok: true, json: async () => ({ aircraft: [aircraft(hex)] }) });

beforeEach(() => {
  vi.useFakeTimers();
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("aircraft feed scope", () => {
  it("clears every feed store and resumes live data when switching to owner mode", () => {
    const { result, rerender } = renderHook(({ owner }) => useAircraftFeed(owner), { initialProps: { owner: false } });
    act(() => {
      Socket.instances[0].open();
      Socket.instances[0].message({
        aircraft: [aircraft("public")],
        detection_arcs: [{ ...aircraft("public"), ambiguity_arc: [[51, -1], [52, -1]], delay_us: 10 }],
        ground_truth: { public: { lat: 51, lon: -1 } },
        ground_truth_meta: { source: "public" },
        anomaly_hexes: ["public"],
      });
      result.current.setPaused(true);
    });
    expect(result.current.aircraft).toHaveLength(1);
    expect(Object.keys(result.current.arcsBufferRef.current)).toHaveLength(1);
    rerender({ owner: true });
    expect(result.current.connected).toBe(false);
    expect(result.current.aircraft).toEqual([]);
    expect(result.current.historyRef.current).toEqual([]);
    for (const name of ["trailsRef", "groundTruthRef", "groundTruthMetaRef", "arcsBufferRef", "detectionsRef"] as const) {
      expect(result.current[name].current).toEqual({});
    }
    expect(result.current.anomalyHexesRef.current.size).toBe(0);
    expect(Socket.instances[1].url).toContain("/ws/aircraft/owner");
    act(() => Socket.instances[1].message({ aircraft: [aircraft("owned")] }));
    expect(result.current.aircraft[0].hex).toBe("owned");
    expect(fetch).toHaveBeenCalledTimes(1); // owner feed never falls back to public HTTP
  });

  it("ignores queued old socket callbacks after a scope switch", () => {
    const { result, rerender } = renderHook(({ owner }) => useAircraftFeed(owner), { initialProps: { owner: false } });
    const old = Socket.instances[0];
    const oldMessage = old.onmessage!;
    const oldOpen = old.onopen!;
    const oldClose = old.onclose!;
    rerender({ owner: true });
    act(() => {
      oldMessage({ data: JSON.stringify({ aircraft: [aircraft("public")] }) });
      oldOpen();
      oldClose();
      vi.advanceTimersByTime(30_000);
    });
    expect(result.current.aircraft).toEqual([]);
    expect(result.current.connected).toBe(false);
    expect(Socket.instances).toHaveLength(2);
  });

  it("ignores an aborted public HTTP response after switching to owner mode", async () => {
    const pending = deferred<ReturnType<typeof response>>();
    vi.mocked(fetch).mockReturnValueOnce(pending.promise as Promise<Response>);
    const { result, rerender } = renderHook(({ owner }) => useAircraftFeed(owner), { initialProps: { owner: false } });
    rerender({ owner: true });
    await act(async () => pending.resolve(response("public")));
    expect(result.current.aircraft).toEqual([]);
    expect(result.current.historyRef.current).toEqual([]);
  });

  it("ignores queued callbacks after unmount", () => {
    const { result, unmount } = renderHook(() => useAircraftFeed());
    const message = Socket.instances[0].onmessage!;
    const history = result.current.historyRef;
    unmount();
    act(() => message({ data: JSON.stringify({ aircraft: [aircraft("late")] }) }));
    expect(history.current).toEqual([]);
  });
});

describe("fallback trails", () => {
  it("bounds trails for an active aircraft without recent_positions", () => {
    const { result } = renderHook(() => useAircraftFeed(true));
    act(() => {
      for (let i = 0; i < 450; i++) Socket.instances[0].message({ aircraft: [aircraft("active", 50 + i * 0.001)] });
    });
    const trail = result.current.trailsRef.current["active"];
    expect(trail).toHaveLength(400);
    expect(trail[0][0]).toBeCloseTo(50.05);
    expect(trail[399][0]).toBeCloseTo(50.449);
  });
});

describe("HTTP fallback ordering", () => {
  it("keeps newer data when an older poll resolves last", async () => {
    const older = deferred<ReturnType<typeof response>>();
    vi.mocked(fetch).mockReturnValueOnce(older.promise as Promise<Response>)
      .mockResolvedValueOnce(response("newer") as Response);
    const { result } = renderHook(() => useAircraftFeed());
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.aircraft[0].hex).toBe("newer");
    await act(async () => older.resolve(response("older")));
    expect(result.current.aircraft[0].hex).toBe("newer");
  });
});

describe("the map's view of who is signed in", () => {
  const ME = { id: "u1", email: "owner@example.invalid", name: "Owner" };
  const ok = (body: unknown) => ({ ok: true, json: async () => body });

  /** Signs `user` into the console and answers the ownership route with `nodes`. */
  function stubAuth(user: unknown, nodes: unknown = ok([])) {
    auth.current = { user, loading: false };
    const fetchMock = vi.fn(async (url: string) => (url.endsWith("/auth/me/nodes") ? nodes : ok({})));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  async function settle() {
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  }

  it("resolves the user and the refs of the nodes they own", async () => {
    // A null ref is an owned node with no registry row, and the map has
    // nothing to match it against.
    stubAuth(ME, ok([{ node_ref: "mine" }, { node_ref: null }]));
    const { result } = renderHook(() => useMapAuth());
    await settle();
    expect(result.current).toEqual({ user: ME, ownedNodeRefs: ["mine"], loading: false });
  });

  it("asks nothing about ownership when nobody is signed in", async () => {
    const fetchMock = stubAuth(null);
    const { result } = renderHook(() => useMapAuth());
    await settle();
    expect(result.current).toEqual({ user: null, ownedNodeRefs: [], loading: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("offers no owner, and asks nothing, where the page withholds the owner view", async () => {
    const fetchMock = stubAuth(ME, ok([{ node_ref: "mine" }]));
    const { result } = renderHook(() => useMapAuth(false));
    await settle();
    expect(result.current).toEqual({ user: null, ownedNodeRefs: [], loading: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("stays loading until the owned nodes arrive", async () => {
    // NodeOwnerControl renders nothing while loading. Releasing it early shows
    // the owner their panel with an ownership count that is still zero.
    const nodes = deferred<unknown>();
    stubAuth(ME, nodes.promise);
    const { result } = renderHook(() => useMapAuth());
    await settle();
    expect(result.current.loading).toBe(true);
    await act(async () => { nodes.resolve(ok([{ node_ref: "mine" }])); });
    expect(result.current).toMatchObject({ loading: false, ownedNodeRefs: ["mine"] });
  });

  it("settles as owning nothing when the ownership request fails", async () => {
    // Ownership left null reads as unsettled, which holds the owner panel in
    // its loading state for the rest of the session.
    stubAuth(ME, { ok: false, status: 500, json: async () => ({ detail: "boom" }) });
    const { result } = renderHook(() => useMapAuth());
    await settle();
    expect(result.current).toEqual({ user: ME, ownedNodeRefs: [], loading: false });
  });
});

describe("the node listing", () => {
  const listing = {
    nodes: {
      "node-a": {
        detection_area: {
          rx: { lat: 51, lon: -1 },
          tx: { lat: 51.2, lon: -1.1 },
          beam_azimuth_deg: 90,
          beam_width_deg: 40,
          max_range_km: 60,
        },
      },
    },
  };
  const analyticsCalls = () =>
    vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/radar/analytics"));

  it("refreshes every 30 s and keeps the last list when a refresh fails", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce({ ok: true, json: async () => listing } as Response)
      .mockResolvedValueOnce({ ok: false, status: 500, json: async () => ({}) } as Response);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { result } = renderHook(() => useNodes("real"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.map((n) => n.node_ref)).toEqual(["node-a"]);
    expect(analyticsCalls()).toHaveLength(1);
    expect(String(analyticsCalls()[0][0])).toContain("real_only=true");

    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(analyticsCalls()).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(analyticsCalls()).toHaveLength(2);
    expect(result.current.map((n) => n.node_ref)).toEqual(["node-a"]);
  });

  it("stops asking once unmounted", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => listing } as Response);
    const { unmount } = renderHook(() => useNodes("real"));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(analyticsCalls()).toHaveLength(1);
  });
});

describe("the owner socket closed by policy", () => {
  // The server closes the owner socket with 1008 once the session behind it no
  // longer resolves. The map asks the session before reopening it.
  const ok = (body: unknown) => ({ ok: true, json: async () => body });
  const status = (code: number) => ({ ok: false, status: code, statusText: "", json: async () => ({ detail: "no" }) });

  /** Answers the owner list with `answer`, and nothing else. */
  function ownerList(answer: () => unknown) {
    const fetchMock = vi.fn(async (url: string) => (url === "/api/auth/me/nodes" ? answer() : new Promise(() => {})));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }
  const listAsks = (fetchMock: ReturnType<typeof ownerList>) =>
    fetchMock.mock.calls.filter(([url]) => url === "/api/auth/me/nodes").length;

  const latest = () => Socket.instances[Socket.instances.length - 1];
  const ownerSockets = () => Socket.instances.filter((s) => s.url.endsWith("/ws/aircraft/owner")).length;

  async function advance(ms: number) {
    await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  }

  /** How long after the current socket closed the next one opens, to the
   *  second, or null when none opens within ten minutes. */
  async function reopensAfter(): Promise<number | null> {
    const count = Socket.instances.length;
    // The session check answers before the backoff starts.
    await advance(0);
    for (let waited = 0; waited < 600_000; waited += 1000) {
      await advance(1000);
      if (Socket.instances.length > count) return waited + 1000;
    }
    return null;
  }

  /** The exact delay: none at `ms - 1`, one at `ms`. */
  async function reopensAtExactly(ms: number) {
    const count = Socket.instances.length;
    await advance(0);
    await advance(ms - 1);
    expect(Socket.instances.length).toBe(count);
    await advance(1);
    expect(Socket.instances.length).toBe(count + 1);
  }

  it("leaves it closed when the session has ended, and tells the console", async () => {
    const fetchMock = ownerList(() => status(401));
    const ended = vi.fn();
    const stop = onSessionEnded(ended);
    renderHook(() => useAircraftFeed(true));
    act(() => latest().open());
    act(() => latest().closedBy(1008));
    expect(await reopensAfter()).toBeNull();
    expect(ownerSockets()).toBe(1);
    expect(listAsks(fetchMock)).toBe(1);
    expect(ended).toHaveBeenCalledTimes(1);
    stop();
  });

  it("reopens it when the session holds, on a backoff an open does not reset", async () => {
    const fetchMock = ownerList(() => ok([{ node_ref: "mine" }]));
    renderHook(() => useAircraftFeed(true));
    // The server accepts every socket and closes it again by policy.
    for (const delay of [3000, 6000, 12_000, 24_000, 30_000, 30_000, 30_000]) {
      act(() => latest().open());
      act(() => latest().closedBy(1008));
      await reopensAtExactly(delay);
      expect(latest().url).toContain("/ws/aircraft/owner");
    }
    // One question per close, none in between.
    expect(listAsks(fetchMock)).toBe(7);
    expect(ownerSockets()).toBe(8);
  });

  it("asks at most every 30 s however long the server keeps that up", async () => {
    const fetchMock = ownerList(() => ok([]));
    renderHook(() => useAircraftFeed(true));
    const closeEach = async () => {
      act(() => latest().open());
      act(() => latest().closedBy(1008));
      await advance(0);
    };
    await closeEach();
    // Ten minutes of a socket closed as soon as it opens.
    for (let t = 0; t < 600_000; t += 500) {
      const before = Socket.instances.length;
      await advance(500);
      if (Socket.instances.length > before) await closeEach();
    }
    // After 3, 6, 12 and 24 s, then every 30 s: 23 sockets in ten minutes,
    // where a backoff that each open reset would make 201.
    expect(ownerSockets()).toBe(23);
    expect(listAsks(fetchMock)).toBe(ownerSockets());
  });

  it.each([
    ["a server error", () => status(503)],
    ["no answer", () => Promise.reject(new TypeError("Failed to fetch"))],
  ])("reopens it on the same backoff when the session check meets %s", async (_, answer) => {
    ownerList(answer);
    renderHook(() => useAircraftFeed(true));
    act(() => latest().open());
    act(() => latest().closedBy(1008));
    await reopensAtExactly(3000);
    act(() => latest().open());
    act(() => latest().closedBy(1008));
    await reopensAtExactly(6000);
  });

  it("reopens nothing when the check answers after the map has gone", async () => {
    let answer!: (value: unknown) => void;
    ownerList(() => new Promise((resolve) => { answer = resolve; }));
    const { unmount } = renderHook(() => useAircraftFeed(true));
    act(() => latest().open());
    act(() => latest().closedBy(1008));
    await advance(0);
    unmount();
    await act(async () => { answer(ok([])); });
    await advance(600_000);
    expect(Socket.instances).toHaveLength(1);
  });

  it("reopens nothing of the owner's when the check answers after the scope changed", async () => {
    let answer!: (value: unknown) => void;
    ownerList(() => new Promise((resolve) => { answer = resolve; }));
    const { rerender } = renderHook(({ owner }) => useAircraftFeed(owner), { initialProps: { owner: true } });
    act(() => latest().open());
    act(() => latest().closedBy(1008));
    await advance(0);
    rerender({ owner: false });
    // The public socket drops twice, so for 6 s there is none: the gap a
    // stale reopen would fill with the owner's.
    act(() => latest().close());
    await advance(3000);
    act(() => latest().close());
    await act(async () => { answer(ok([])); });
    await advance(600_000);
    expect(ownerSockets()).toBe(1);
    // The public feed this page shows (the mocked host takes the full one).
    expect(latest().url).toMatch(/\/ws\/aircraft$/);
  });
});

describe("any other close", () => {
  const latest = () => Socket.instances[Socket.instances.length - 1];

  async function reopensAtExactly(ms: number) {
    const count = Socket.instances.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(ms - 1); });
    expect(Socket.instances.length).toBe(count);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(Socket.instances.length).toBe(count + 1);
  }

  it.each([
    ["the owner socket dropped (1006)", true, 1006],
    ["the owner socket going away (1001)", true, 1001],
    ["the owner socket closed with no code", true, undefined],
    ["the public socket closed by policy (1008)", false, 1008],
  ])("keeps today's backoff for %s, and asks nothing", async (_, owner, code) => {
    renderHook(() => useAircraftFeed(owner, "real"));
    const close = () => act(() => (code === undefined ? latest().close() : latest().closedBy(code)));
    // Never opened: 3 s, then 6 s.
    close();
    await reopensAtExactly(3000);
    close();
    await reopensAtExactly(6000);
    // An open resets it.
    act(() => latest().open());
    close();
    await reopensAtExactly(3000);
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith("/api/auth/"))).toEqual([]);
  });
});

describe("the owner list the map reads", () => {
  const ME = { id: "u1", email: "owner@example.invalid", name: "Owner" };
  const ok = (body: unknown) => ({ ok: true, json: async () => body });

  it("is asked through the console's client, so a 401 there ends the session", async () => {
    auth.current = { user: ME, loading: false };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 401, json: async () => ({ detail: "Not authenticated" }) })));
    const ended = vi.fn();
    const stop = onSessionEnded(ended);
    const { result } = renderHook(() => useMapAuth());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(ended).toHaveBeenCalledTimes(1);
    expect(result.current).toEqual({ user: ME, ownedNodeRefs: [], loading: false });
    stop();
  });

  it("takes in the list the owner socket's session check reads", async () => {
    auth.current = { user: ME, loading: false };
    let owned = [{ node_ref: "mine" }];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url === "/api/auth/me/nodes" ? ok(owned) : new Promise(() => {}))));
    const { result } = renderHook(() => ({ auth: useMapAuth(), feed: useAircraftFeed(true) }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.auth.ownedNodeRefs).toEqual(["mine"]);

    // A node joined the account meanwhile; the next list says so.
    owned = [{ node_ref: "mine" }, { node_ref: "new" }];
    const socket = Socket.instances[Socket.instances.length - 1];
    act(() => socket.open());
    act(() => socket.closedBy(1008));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current.auth.ownedNodeRefs).toEqual(["mine", "new"]);
  });
});
