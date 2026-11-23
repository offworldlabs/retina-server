import { HttpError, UnauthorizedError } from "@retina/shared";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ownerRegistry, useNodeRegistry } from "../../pages/user/dataExplorer/useNodeRegistry";
import { ownedNode } from "../fixtures/ownedNode";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@retina/shared")>()),
  request: requestMock,
}));

afterEach(() => requestMock.mockReset());

const payload = (nodes: Record<string, unknown>) => ({ nodes });

const located = {
  name: "London",
  status: "online",
  is_synthetic: false,
  location: {
    rx_lat: 51.5074,
    rx_lon: -0.1278,
    location_uncertainty_km: 1.5,
  },
};

describe("useNodeRegistry", () => {
  it("asks the radar for the fleet, sending only the origin as the referrer", async () => {
    requestMock.mockResolvedValue(payload({}));
    renderHook(() => useNodeRegistry());
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    expect(requestMock.mock.calls[0][0]).toBe("/api/radar/nodes");
    // The page's address can name a node no public route may be told of.
    expect(requestMock.mock.calls[0][1]).toEqual({ referrerPolicy: "strict-origin" });
  });

  it("maps a located node", async () => {
    requestMock.mockResolvedValue(payload({ "ret-london": located }));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.nodes.size).toBe(1));
    expect(result.current.nodes.get("ret-london")).toEqual({
      id: "ret-london",
      name: "London",
      status: "online",
      synthetic: false,
      lat: 51.5074,
      lon: -0.1278,
      uncertaintyKm: 1.5,
    });
    // An entitled viewer is answered by the radar alone.
    expect(requestMock).toHaveBeenCalledTimes(1);
  });

  it("names a node after its id when the payload has no name", async () => {
    requestMock.mockResolvedValue(payload({ "ret-anon": { ...located, name: undefined } }));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.nodes.size).toBe(1));
    expect(result.current.nodes.get("ret-anon")!.name).toBe("ret-anon");
  });

  it("treats a half-published position as no position", async () => {
    requestMock.mockResolvedValue(
      payload({ "ret-half": { ...located, location: { rx_lat: 51.5 } } }),
    );
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.nodes.size).toBe(1));
    const node = result.current.nodes.get("ret-half")!;
    expect(node.lat).toBeNull();
    expect(node.lon).toBeNull();
  });

  it("trusts the server flag over a synthetic-looking id", async () => {
    requestMock.mockResolvedValue(payload({ "synth-9": { ...located, is_synthetic: false } }));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.nodes.size).toBe(1));
    expect(result.current.nodes.get("synth-9")!.synthetic).toBe(false);
  });

  it("surfaces a failure instead of presenting an empty fleet as the truth", async () => {
    requestMock.mockRejectedValue(new Error("HTTP 503"));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.error).toBe("HTTP 503"));
    expect(result.current.nodes.size).toBe(0);
    expect(result.current.loading).toBe(false);
  });

  it("clears the error and repopulates on retry", async () => {
    requestMock.mockRejectedValueOnce(new Error("HTTP 503"));
    requestMock.mockResolvedValueOnce(payload({ "ret-london": located }));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.error).toBe("HTTP 503"));
    act(() => result.current.retry());

    await waitFor(() => expect(result.current.nodes.size).toBe(1));
    expect(result.current.error).toBeNull();
    expect(result.current.nodes.has("ret-london")).toBe(true);
  });
});

describe("useNodeRegistry, without API access", () => {
  // A node as /api/map/nodes gives it: the radar's location block, no name.
  const mapped = {
    node_ref: "ndelondon000001",
    status: "online",
    is_synthetic: false,
    location: { rx_lat: 51.5074, rx_lon: -0.1278, location_uncertainty_km: 1.5 },
    detection_area: null,
    empirical_coverage: null,
  };

  /** The radar and the public map each answering, or failing with an Error. */
  const answers = (radar: unknown, map: unknown) =>
    requestMock.mockImplementation((path: string) => {
      const answer = path === "/api/radar/nodes" ? radar : map;
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
    });

  it.each([
    ["403, an account without access", new HttpError(403, "Forbidden", { detail: { reason: "not_entitled" } })],
    ["401, no session", new UnauthorizedError({ detail: { reason: "missing_credentials" } })],
  ])("takes positions from the public map when the radar refuses with %s", async (_, refusal) => {
    answers(refusal, payload({ ndelondon000001: mapped }));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.nodes.size).toBe(1));
    expect(requestMock.mock.calls).toEqual([
      ["/api/radar/nodes", { referrerPolicy: "strict-origin" }],
      ["/api/map/nodes", { referrerPolicy: "strict-origin" }],
    ]);
    expect(result.current.nodes.get("ndelondon000001")).toEqual({
      id: "ndelondon000001",
      name: "ndelondon000001",
      status: "online",
      synthetic: false,
      lat: 51.5074,
      lon: -0.1278,
      uncertaintyKm: 1.5,
    });
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("holds a server error from the radar rather than asking the map", async () => {
    answers(new HttpError(500, "Internal Server Error", null), payload({ ndelondon000001: mapped }));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.error).toBe("500 Internal Server Error"));
    expect(requestMock).toHaveBeenCalledTimes(1);
    expect(result.current.nodes.size).toBe(0);
  });

  it("shows the map's own failure when the fallback fails too", async () => {
    answers(new HttpError(403, "Forbidden", null), new HttpError(503, "Service Unavailable", null));
    const { result } = renderHook(() => useNodeRegistry());

    await waitFor(() => expect(result.current.error).toBe("503 Service Unavailable"));
    expect(result.current.loading).toBe(false);
  });
});

describe("useNodeRegistry, not wanted", () => {
  it("asks for nothing and settles empty", async () => {
    const { result } = renderHook(() => useNodeRegistry(false));
    await act(async () => {});
    expect(requestMock).not.toHaveBeenCalled();
    expect(result.current.nodes.size).toBe(0);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("reads as loading, not as an empty fleet, once it is wanted", async () => {
    requestMock.mockReturnValue(new Promise(() => {}));
    const { result, rerender } = renderHook(({ on }) => useNodeRegistry(on), { initialProps: { on: false } });
    await act(async () => {});
    rerender({ on: true });
    expect(result.current.loading).toBe(true);
    expect(requestMock).toHaveBeenCalledWith("/api/radar/nodes", { referrerPolicy: "strict-origin" });
  });
});

describe("ownerRegistry", () => {
  const at = (lat: number | null, lon: number | null) => ({
    rx_lat: lat,
    rx_lon: lon,
    rx_alt_ft: null,
    tx_lat: null,
    tx_lon: null,
    tx_alt_ft: null,
  });

  it("keys the owner's nodes by ref, at their true position, private ones included", () => {
    const roof = ownedNode({
      node_id: "retroof0001",
      node_ref: "nderoof00000001",
      name: "Roof",
      status: "active",
      location_private: true,
      location: at(51.41234, -0.30123),
    });
    expect(ownerRegistry([roof]).get("nderoof00000001")).toEqual({
      id: "nderoof00000001",
      name: "Roof",
      status: "active",
      synthetic: false,
      lat: 51.41234,
      lon: -0.30123,
      uncertaintyKm: null,
    });
  });

  it("leaves out a node with no ref, which no archive key could name", () => {
    expect(ownerRegistry([ownedNode({ node_ref: null })]).size).toBe(0);
  });

  it("treats half a position as none, and names a node with no name by its ref", () => {
    const node = ownerRegistry([ownedNode({ node_ref: "ndehalf", name: null, location: at(51.5, null) })]).get("ndehalf");
    expect(node).toMatchObject({ name: "ndehalf", lat: null, lon: null });
  });
});
