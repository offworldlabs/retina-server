import { act, renderHook } from "@testing-library/react";
import { HttpError, UnauthorizedError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { useOwnedNodes } from "../hooks/useOwnedNodes";
import type { OwnedNode } from "../types";

vi.mock("../api/client", () => ({ api: { myNodes: vi.fn() } }));

type Auth = { user: { id: string; email: string } | null; loading: boolean; session?: number };
const state = vi.hoisted(() => ({ auth: { user: null, loading: false } as Auth }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const ADA = { id: "u-ada", email: "ada@example.com" };
const BOB = { id: "u-bob", email: "bob@example.com" };

const node = (fields: Partial<OwnedNode>) => fields as OwnedNode;
const ROOF = node({ node_id: "ret1a2b3c4d", node_ref: "nde0123456789", name: "Roof", location_private: true });
const MAST = node({ node_id: "ret5e6f7a8b", node_ref: "ndeabcdefabcd", name: "Mast", location_private: false });
// Not published anywhere yet, so there is nothing to mark it by.
const BENCH = node({ node_id: "ret9c0d1e2f", node_ref: null, name: null, location_private: false });
const BOBS = node({ node_id: "ret3a4b5c6d", node_ref: "ndebbbbbbbbbb", name: "Bob's" });

/** A promise the test settles by hand, so an in-flight state can be seen. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Lets the settled promises inside the hook reach setState. */
const flush = () => act(async () => {});

const realLocation = window.location;

function onHost(hostname: string) {
  Object.defineProperty(window, "location", {
    value: { hostname, pathname: "/", search: "", hash: "", href: `https://${hostname}/` },
    writable: true,
    configurable: true,
  });
}

beforeEach(() => {
  vi.mocked(api.myNodes).mockReset();
  state.auth = { user: null, loading: false };
  // usePolling logs every failure.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

describe("useOwnedNodes", () => {
  it("asks nothing for a caller with no session", async () => {
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("signed-out");
    expect(result.current.nodes).toEqual([]);
    expect(result.current.refs.size).toBe(0);
    expect(result.current.error).toBeNull();
    expect(api.myNodes).not.toHaveBeenCalled();
  });

  it.each([
    ["while the session is still being settled", { user: null, loading: true }],
    ["with a session", { user: ADA, loading: false }],
  ])("asks nothing on the admin console, %s", async (_when, auth) => {
    onHost("admin.retina.fm");
    state.auth = auth;
    vi.mocked(api.myNodes).mockResolvedValue([ROOF]);
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("signed-out");
    expect(result.current.nodes).toEqual([]);
    expect(api.myNodes).not.toHaveBeenCalled();
  });

  it("asks on the app host, which the admin test above differs from only by name", async () => {
    onHost("app.retina.fm");
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue([ROOF]);
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("ready");
    expect(api.myNodes).toHaveBeenCalledTimes(1);
  });

  it("is loading while the session is settled, and asks once it is", async () => {
    state.auth = { user: null, loading: true };
    vi.mocked(api.myNodes).mockResolvedValue([ROOF]);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("loading");
    expect(result.current.nodes).toEqual([]);
    expect(api.myNodes).not.toHaveBeenCalled();

    state.auth = { user: ADA, loading: false };
    rerender();
    expect(result.current.status).toBe("loading");
    expect(api.myNodes).toHaveBeenCalledTimes(1);

    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF]);
  });

  it("lists the owner's nodes, and the refs of those that have one", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue([ROOF, MAST, BENCH]);
    const { result } = renderHook(() => useOwnedNodes());

    expect(result.current.status).toBe("loading");
    expect(result.current.nodes).toEqual([]);

    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF, MAST, BENCH]);
    expect([...result.current.refs]).toEqual(["nde0123456789", "ndeabcdefabcd"]);
    expect(result.current.error).toBeNull();
  });

  it("is ready with no nodes for an owner who has none", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue([]);
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([]);
  });

  it.each([
    ["an object", { detail: "Not found" }],
    ["null", null],
    ["a string", "<!doctype html>"],
  ])("reads %s for an answer as no nodes", async (_what, answer) => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue(answer as never);
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([]);
    expect(result.current.refs.size).toBe(0);
  });

  it("reports a first failure as an error, and refresh asks again", async () => {
    state.auth = { user: ADA, loading: false };
    const retry = deferred<OwnedNode[]>();
    vi.mocked(api.myNodes).mockRejectedValueOnce(new Error("502 Bad Gateway")).mockReturnValueOnce(retry.promise);
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("error");
    expect(result.current.error?.message).toBe("502 Bad Gateway");
    expect(result.current.nodes).toEqual([]);

    act(() => result.current.refresh());
    expect(api.myNodes).toHaveBeenCalledTimes(2);
    // The retry is in flight, so the old failure is not its answer.
    expect(result.current.status).toBe("loading");
    expect(result.current.error).toBeNull();

    retry.resolve([ROOF]);
    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF]);
    expect(result.current.error).toBeNull();
  });

  it("keeps the last list through a failed refresh, and says it failed", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValueOnce([ROOF, MAST]).mockRejectedValueOnce(new Error("timed out"));
    const { result } = renderHook(() => useOwnedNodes());
    await flush();
    expect(result.current.status).toBe("ready");

    act(() => result.current.refresh());
    // Still the owner's list while the refresh is out.
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF, MAST]);
    await flush();

    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF, MAST]);
    expect([...result.current.refs]).toEqual(["nde0123456789", "ndeabcdefabcd"]);
    expect(result.current.error?.message).toBe("timed out");
  });

  // The session ended elsewhere: signed out in another tab, expired, revoked.
  // The context above is held still, so the hook is seen on its own; the
  // real one drops the identity too (sessionEnded.test.tsx).
  it("drops the list when the minute's refresh answers 401, rather than keep it", async () => {
    vi.useFakeTimers();
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValueOnce([ROOF, MAST]).mockRejectedValueOnce(new UnauthorizedError());
    const { result } = renderHook(() => useOwnedNodes());
    await flush();
    expect(result.current.status).toBe("ready");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(api.myNodes).toHaveBeenCalledTimes(2);
    expect(result.current.status).toBe("signed-out");
    expect(result.current.nodes).toEqual([]);
    expect(result.current.refs.size).toBe(0);
    expect(result.current.error).toBeNull();
  });

  it("reads a 401 on the first ask as signed out, not as a failure", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockRejectedValue(new UnauthorizedError());
    const { result } = renderHook(() => useOwnedNodes());
    await flush();

    expect(result.current.status).toBe("signed-out");
    expect(result.current.nodes).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it("stays signed out through a retry after a 401, until the retry answers", async () => {
    state.auth = { user: ADA, loading: false };
    const retry = deferred<OwnedNode[]>();
    vi.mocked(api.myNodes)
      .mockResolvedValueOnce([ROOF])
      .mockRejectedValueOnce(new UnauthorizedError())
      .mockReturnValueOnce(retry.promise);
    const { result } = renderHook(() => useOwnedNodes());
    await flush();
    act(() => result.current.refresh());
    await flush();
    expect(result.current.status).toBe("signed-out");

    act(() => result.current.refresh());
    // The 401 is the last word until the retry has one: not the list before it.
    expect(result.current.status).toBe("signed-out");
    expect(result.current.nodes).toEqual([]);

    retry.resolve([ROOF]);
    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF]);
  });

  it("does not hold one owner's 401 against whoever signs in next", async () => {
    state.auth = { user: ADA, loading: false };
    const bobs = deferred<OwnedNode[]>();
    vi.mocked(api.myNodes).mockRejectedValueOnce(new UnauthorizedError()).mockReturnValueOnce(bobs.promise);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();
    expect(result.current.status).toBe("signed-out");

    state.auth = { user: BOB, loading: false };
    rerender();
    expect(result.current.status).toBe("loading");

    bobs.resolve([BOBS]);
    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([BOBS]);
  });

  // A session adopted in place, as a mailed link redeemed without a page load
  // does, back into the same account: what the session before was told is not
  // this one's answer (sessionEndedAfterSignIn.test.tsx has the real provider).
  it("does not hold a 401 to the session before against the same owner's next session", async () => {
    state.auth = { user: ADA, loading: false, session: 0 };
    const next = deferred<OwnedNode[]>();
    vi.mocked(api.myNodes).mockRejectedValueOnce(new UnauthorizedError()).mockReturnValueOnce(next.promise);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();
    expect(result.current.status).toBe("signed-out");

    state.auth = { user: ADA, loading: false, session: 1 };
    rerender();
    expect(api.myNodes).toHaveBeenCalledTimes(2);
    expect(result.current.status).toBe("loading");

    next.resolve([ROOF]);
    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF]);
  });

  it("drops a 401 that answers the session before once the same owner's next has asked", async () => {
    state.auth = { user: ADA, loading: false, session: 0 };
    const late = deferred<OwnedNode[]>();
    vi.mocked(api.myNodes)
      .mockResolvedValueOnce([ROOF, MAST])
      .mockReturnValueOnce(late.promise)
      .mockResolvedValueOnce([ROOF, MAST]);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();
    act(() => result.current.refresh());

    state.auth = { user: ADA, loading: false, session: 1 };
    rerender();
    await flush();
    expect(api.myNodes).toHaveBeenCalledTimes(3);
    expect(result.current.status).toBe("ready");

    late.reject(new UnauthorizedError());
    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF, MAST]);
  });

  // Only a 401 says the session is gone; a 403 is an answer about the request.
  it("still reports any other refused refresh as a failure, with the last list", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValueOnce([ROOF]).mockRejectedValueOnce(new HttpError(403, "Forbidden", null));
    const { result } = renderHook(() => useOwnedNodes());
    await flush();
    act(() => result.current.refresh());
    await flush();

    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([ROOF]);
    expect(result.current.error?.message).toBe("403 Forbidden");
  });

  it("asks again for whoever signs in next, and never shows them the last owner's nodes", async () => {
    state.auth = { user: ADA, loading: false };
    const bobs = deferred<OwnedNode[]>();
    vi.mocked(api.myNodes).mockResolvedValueOnce([ROOF]).mockReturnValueOnce(bobs.promise);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();
    expect(result.current.nodes).toEqual([ROOF]);

    // Another account taken over in place, with no signed-out render between.
    state.auth = { user: BOB, loading: false };
    rerender();
    expect(api.myNodes).toHaveBeenCalledTimes(2);
    expect(result.current.status).toBe("loading");
    expect(result.current.nodes).toEqual([]);
    expect(result.current.refs.size).toBe(0);

    bobs.resolve([BOBS]);
    await flush();
    expect(result.current.status).toBe("ready");
    expect(result.current.nodes).toEqual([BOBS]);

    state.auth = { user: null, loading: false };
    rerender();
    await flush();
    expect(result.current.status).toBe("signed-out");
    expect(result.current.nodes).toEqual([]);
    expect(api.myNodes).toHaveBeenCalledTimes(2);
  });

  it("does not ask again when the same user is handed over as a new object", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue([ROOF]);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();

    state.auth = { user: { ...ADA }, loading: false };
    rerender();
    await flush();
    expect(result.current.status).toBe("ready");
    expect(api.myNodes).toHaveBeenCalledTimes(1);
  });

  it("keeps the same objects across a render with nothing new", async () => {
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue([ROOF, MAST]);
    const { result, rerender } = renderHook(() => useOwnedNodes());
    await flush();
    const first = result.current;

    rerender();
    expect(result.current).toBe(first);
    expect(result.current.refs).toBe(first.refs);
    expect(result.current.nodes).toBe(first.nodes);
  });

  it("asks again every minute by default, and on the period it is given", async () => {
    vi.useFakeTimers();
    state.auth = { user: ADA, loading: false };
    vi.mocked(api.myNodes).mockResolvedValue([ROOF]);
    const minute = renderHook(() => useOwnedNodes());
    await flush();
    expect(api.myNodes).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_999);
    });
    expect(api.myNodes).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.myNodes).toHaveBeenCalledTimes(2);
    minute.unmount();

    vi.mocked(api.myNodes).mockClear();
    const once = renderHook(() => useOwnedNodes(0));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(api.myNodes).toHaveBeenCalledTimes(1);
    expect(once.result.current.status).toBe("ready");
  });

  it("runs no timer for a caller with no session", async () => {
    vi.useFakeTimers();
    renderHook(() => useOwnedNodes(1000));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(api.myNodes).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
