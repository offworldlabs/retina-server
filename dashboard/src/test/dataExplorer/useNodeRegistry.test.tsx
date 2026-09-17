import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useNodeRegistry } from "../../pages/user/dataExplorer/useNodeRegistry";

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
  it("asks the radar for the fleet", async () => {
    requestMock.mockResolvedValue(payload({}));
    renderHook(() => useNodeRegistry());
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    expect(requestMock.mock.calls[0][0]).toBe("/api/radar/nodes");
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
