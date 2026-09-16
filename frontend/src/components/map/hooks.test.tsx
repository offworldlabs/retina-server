import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAircraftFeed, useAuth } from "./hooks";

vi.mock("../../utils/domains", () => ({ hidesRealNodes: false, usesRealOnlyFeed: false }));

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { Socket.instances.push(this); }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
  close() { this.readyState = 3; this.onclose?.(); }
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
  const unauthorized = { ok: false, status: 401, json: async () => ({ detail: "Not authenticated" }) };

  /** Answers the identity route with `me` and the ownership route with `nodes`. */
  function stubAuth(me: unknown, nodes: unknown = []) {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith("/auth/me") ? me : url.endsWith("/auth/me/nodes") ? ok(nodes) : ok({})
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  async function settle() {
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  }

  it("resolves the user and the refs of the nodes they own", async () => {
    // A null ref is an owned node with no registry row, and the map has
    // nothing to match it against.
    stubAuth(ok(ME), [{ node_ref: "mine" }, { node_ref: null }]);
    const { result } = renderHook(() => useAuth());
    await settle();
    expect(result.current).toEqual({ user: ME, ownedNodeRefs: ["mine"], loading: false });
  });

  it("asks nothing about ownership when nobody is signed in", async () => {
    const fetchMock = stubAuth(unauthorized);
    const { result } = renderHook(() => useAuth());
    await settle();
    expect(result.current).toEqual({ user: null, ownedNodeRefs: [], loading: false });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/auth/me"]);
  });

  it("stays loading until the owned nodes arrive", async () => {
    // NodeOwnerControl renders nothing while loading. Releasing it early shows
    // the owner their panel with an ownership count that is still zero.
    const nodes = deferred<unknown>();
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url.endsWith("/auth/me") ? ok(ME) : nodes.promise
    ));
    const { result } = renderHook(() => useAuth());
    await settle();
    expect(result.current.loading).toBe(true);
    await act(async () => { nodes.resolve(ok([{ node_ref: "mine" }])); });
    expect(result.current).toMatchObject({ loading: false, ownedNodeRefs: ["mine"] });
  });

  it("retries an identity the server did not answer", async () => {
    // The map is often the first page open when a droplet comes back up, and
    // one unanswered call used to settle it as a signed-out visitor for the
    // rest of the session.
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockImplementation(async (url: string) =>
        url.endsWith("/auth/me") ? ok(ME) : ok([{ node_ref: "mine" }])
      );
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useAuth());
    await settle();
    expect(result.current.user).toEqual(ME);
  });
});
