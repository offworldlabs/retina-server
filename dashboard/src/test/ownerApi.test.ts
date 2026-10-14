import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ownerArchivePath } from "../api/client";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@retina/shared")>()),
  request: requestMock,
}));

/** A node id with characters that must not reach the path as they are. */
const ID = "ret/a b?1";
const ENC = "ret%2Fa%20b%3F1";

beforeEach(() => {
  requestMock.mockReset();
  requestMock.mockResolvedValue({});
});

/** The path and options of the one request the last call made. */
function sent(): [string, { method?: string; body?: string; signal?: AbortSignal } | undefined] {
  expect(requestMock).toHaveBeenCalledTimes(1);
  return requestMock.mock.calls[0] as never;
}

describe("owner node routes", () => {
  it("lists the caller's nodes with a GET", async () => {
    await api.myNodes();
    expect(sent()).toEqual(["/api/auth/me/nodes", undefined]);
  });

  it("reads one node, encoding its id", async () => {
    await api.myNode(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}`, undefined]);
  });

  it("patches one node with the patch as the JSON body", async () => {
    await api.updateMyNode(ID, { name: "Rooftop", location_private: true });
    const [path, opts] = sent();
    expect(path).toBe(`/api/auth/me/nodes/${ENC}`);
    expect(opts?.method).toBe("PATCH");
    expect(JSON.parse(opts?.body ?? "")).toEqual({ name: "Rooftop", location_private: true });
  });

  it("sends a null name, which clears the label, and only the keys given", async () => {
    await api.updateMyNode("ret-a", { name: null });
    expect(sent()[1]?.body).toBe('{"name":null}');
  });

  it("releases a node with a DELETE on its claim", async () => {
    await api.releaseNode(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}/claim`, { method: "DELETE" }]);
  });

  it("reads the owner's aircraft feed", async () => {
    await api.myAircraft();
    expect(sent()).toEqual(["/api/auth/me/aircraft", undefined]);
  });
});

describe("owner node readings", () => {
  it("reads the analytics of every owned node", async () => {
    await api.myAnalytics();
    expect(sent()).toEqual(["/api/auth/me/analytics", undefined]);
  });

  it("reads one node's analytics", async () => {
    await api.myNodeAnalytics(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}/analytics`, undefined]);
  });

  it("passes through the null of a node with no analytics yet", async () => {
    requestMock.mockResolvedValue(null);
    await expect(api.myNodeAnalytics("ret-a")).resolves.toBeNull();
  });

  it("reads one node's last heartbeat", async () => {
    await api.myNodeReport(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}/report`, undefined]);
  });

  it("passes through the null of a node that never sent one", async () => {
    requestMock.mockResolvedValue(null);
    await expect(api.myNodeReport("ret-a")).resolves.toBeNull();
  });

  it("reads the config history, 50 versions by default", async () => {
    await api.myNodeConfigHistory(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}/config-history?limit=50`, undefined]);
  });

  it("reads the config history with the limit given", async () => {
    await api.myNodeConfigHistory("ret-a", 200);
    expect(sent()[0]).toBe("/api/auth/me/nodes/ret-a/config-history?limit=200");
  });

  it("reads the events, 100 by default", async () => {
    await api.myNodeEvents(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}/events?limit=100`, undefined]);
  });

  it("reads the events with the limit given", async () => {
    await api.myNodeEvents("ret-a", 500);
    expect(sent()[0]).toBe("/api/auth/me/nodes/ret-a/events?limit=500");
  });

  it("reads the SNR series, 24 hours by default", async () => {
    await api.myNodeSnr(ID);
    expect(sent()).toEqual([`/api/auth/me/nodes/${ENC}/snr?hours=24`, undefined]);
  });

  it("reads the SNR series over the hours given", async () => {
    await api.myNodeSnr("ret-a", 72);
    expect(sent()[0]).toBe("/api/auth/me/nodes/ret-a/snr?hours=72");
  });
});

describe("owner archive", () => {
  it("lists with no query string when nothing is asked", async () => {
    await api.myArchive({});
    expect(sent()).toEqual(["/api/auth/me/archive", undefined]);
  });

  it("sends date, node_ref, limit and offset under the server's names", async () => {
    await api.myArchive({ date: "2026/09/17", nodeRef: "ret-a", limit: 25, offset: 50 });
    const [path] = sent();
    const url = new URL(path, "http://x");
    expect(url.pathname).toBe("/api/auth/me/archive");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      date: "2026/09/17",
      node_ref: "ret-a",
      limit: "25",
      offset: "50",
    });
  });

  it("omits the params that are absent or null", async () => {
    await api.myArchive({ nodeRef: null, limit: 10 });
    expect(sent()[0]).toBe("/api/auth/me/archive?limit=10");
  });

  it("keeps a zero offset, which is a value", async () => {
    await api.myArchive({ offset: 0 });
    expect(sent()[0]).toBe("/api/auth/me/archive?offset=0");
  });

  it("hands the request options on", async () => {
    const ctrl = new AbortController();
    await api.myArchive({}, { signal: ctrl.signal });
    expect(sent()[1]?.signal).toBe(ctrl.signal);
  });

  it("reads one file by its key, unencoded", async () => {
    const key = "year=2026/month=09/day=17/node_ref=ret-a/part-0.parquet";
    await api.myArchiveFile(key);
    expect(sent()).toEqual([`/api/auth/me/archive/${key}`, undefined]);
  });

  it("hands the request options on for a file", async () => {
    const ctrl = new AbortController();
    await api.myArchiveFile("k", { signal: ctrl.signal });
    expect(sent()).toEqual(["/api/auth/me/archive/k", { signal: ctrl.signal }]);
  });

  it("builds the file path from the key as the public archive does", () => {
    expect(ownerArchivePath("year=2026/node_ref=ret-a/p.parquet")).toBe(
      "/api/auth/me/archive/year=2026/node_ref=ret-a/p.parquet",
    );
  });
});

describe("leaderboard", () => {
  it("is read from the radar routes", async () => {
    await api.leaderboard();
    expect(sent()).toEqual(["/api/radar/leaderboard", undefined]);
  });
});
