import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ownerRegistry, useNodeRegistry } from "../../pages/user/dataExplorer/useNodeRegistry";
import { ownedNode } from "../fixtures/ownedNode";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));

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
