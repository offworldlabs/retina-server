import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NodeMap } from "../../pages/user/dataExplorer/NodeMap";
import type { RegistryNode } from "../../pages/user/dataExplorer/nodes";
import {
  DEFAULT_RADIUS_KM,
  defaultFilters,
  type ExplorerFilters,
} from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";

function node(id: string, name: string, lat: number | null, lon: number | null): RegistryNode {
  return { id, name, status: "online", synthetic: false, lat, lon, uncertaintyKm: null };
}

const REGISTRY = new Map<string, RegistryNode>([
  ["ret-london", node("ret-london", "London", 51.5074, -0.1278)],
  ["ret-oxford", node("ret-oxford", "Oxford", 51.752, -1.2577)],
  ["ret-nowhere", node("ret-nowhere", "Nowhere", null, null)],
]);

/** jsdom lays nothing out, so the map's box has to be declared for a click to
 *  mean a position. */
function sized(element: Element) {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue({
    left: 0,
    top: 0,
    width: 900,
    height: 300,
    right: 900,
    bottom: 300,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
}

function setup(over: Partial<ExplorerFilters> = {}, effective?: Set<string>) {
  const onChange = vi.fn();
  const filters = { ...defaultFilters(TODAY), ...over };
  const { container } = render(
    <NodeMap
      filters={filters}
      nodes={REGISTRY}
      effective={effective ?? new Set(REGISTRY.keys())}
      radiusKm={DEFAULT_RADIUS_KM}
      onChange={onChange}
    />,
  );
  return { onChange, container };
}

const nearFrom = (onChange: ReturnType<typeof vi.fn>) => {
  const { calls } = onChange.mock;
  return (calls[calls.length - 1][0] as ExplorerFilters).near;
};

describe("NodeMap", () => {
  it("draws a marker for every node that publishes a position", () => {
    setup();
    expect(screen.getByRole("button", { name: /London/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Oxford/ })).toBeInTheDocument();
  });

  it("draws no marker for a node with no position, and says how many", () => {
    setup();
    expect(screen.queryByRole("button", { name: /Nowhere/ })).not.toBeInTheDocument();
    expect(screen.getByText(/1 node without a published position/)).toBeInTheDocument();
  });

  it("says there is nothing to draw when no node can be placed", () => {
    const onChange = vi.fn();
    render(
      <NodeMap
        filters={defaultFilters(TODAY)}
        nodes={new Map([["ret-nowhere", node("ret-nowhere", "Nowhere", null, null)]])}
        effective={new Set(["ret-nowhere"])}
        radiusKm={DEFAULT_RADIUS_KM}
        onChange={onChange}
      />,
    );
    expect(screen.getByText(/no node publishes a position/i)).toBeInTheDocument();
  });

  it("does not call an unarrived fleet an unplaceable one", () => {
    render(
      <NodeMap
        filters={defaultFilters(TODAY)}
        nodes={new Map()}
        effective={new Set()}
        radiusKm={DEFAULT_RADIUS_KM}
        loading
        onChange={vi.fn()}
      />,
    );
    expect(screen.queryByText(/no node publishes a position/i)).not.toBeInTheDocument();
  });

  it("centres on a node when its marker is clicked, keeping the current radius", () => {
    const { onChange } = setup({ near: { lat: 0, lon: 0, km: 25 } });
    fireEvent.click(screen.getByRole("button", { name: /Oxford/ }));
    expect(nearFrom(onChange)).toEqual({ lat: 51.752, lon: -1.2577, km: 25 });
  });

  it("centres from the keyboard, since a marker takes focus", () => {
    const { onChange } = setup({ near: null });
    fireEvent.keyDown(screen.getByRole("button", { name: /Oxford/ }), { key: "Enter" });
    expect(nearFrom(onChange)).toEqual({ lat: 51.752, lon: -1.2577, km: DEFAULT_RADIUS_KM });
  });

  it("centres on the space bar too, as a button would", () => {
    const { onChange } = setup({ near: null });
    fireEvent.keyDown(screen.getByRole("button", { name: /London/ }), { key: " " });
    expect(nearFrom(onChange)!.lat).toBe(51.5074);
  });

  it("opens at a neighbourhood radius rather than the smallest one allowed", () => {
    const { onChange } = setup({ near: null });
    fireEvent.click(screen.getByRole("button", { name: /Oxford/ }));
    expect(nearFrom(onChange)!.km).toBe(DEFAULT_RADIUS_KM);
  });

  it("gives a centre the radius chosen before there was one to apply it to", () => {
    const onChange = vi.fn();
    render(
      <NodeMap
        filters={defaultFilters(TODAY)}
        nodes={REGISTRY}
        effective={new Set(REGISTRY.keys())}
        radiusKm={12}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Oxford/ }));
    expect(nearFrom(onChange)!.km).toBe(12);
  });

  it("centres on the point clicked on the map itself", () => {
    const { onChange, container } = setup({ near: null });
    const svg = container.querySelector("svg")!;
    sized(svg);
    fireEvent.click(svg, { clientX: 450, clientY: 150 });

    const near = nearFrom(onChange)!;
    // The middle of the box is the middle of the fitted extent.
    expect(near.lat).toBeCloseTo((51.5074 + 51.752) / 2, 2);
    expect(near.lon).toBeCloseTo((-0.1278 + -1.2577) / 2, 2);
  });

  it("marks a node the filters have excluded, so the drawing agrees with the list", () => {
    const { container } = setup({}, new Set(["ret-london"]));
    const oxford = screen.getByRole("button", { name: /Oxford/ });
    expect(oxford.getAttribute("class")).toContain("de-map-faded");
    // The node's own label fades with it.
    expect(container.querySelectorAll(".de-map-faded")).toHaveLength(2);
  });

  it("draws a graticule with its degrees written on it", () => {
    const { container } = setup();
    expect(container.querySelectorAll(".de-map-grid").length).toBeGreaterThan(1);
    expect(container.querySelector(".de-map-gridlab")!.textContent).toMatch(/°$/);
  });

  it("names each dot, so the map reads without hovering every one", () => {
    const { container } = setup();
    const labels = Array.from(container.querySelectorAll(".de-map-nlab"), (l) => l.textContent);
    expect(labels).toContain("ret-london");
    expect(labels).toContain("ret-oxford");
  });

  it("carries the published position and its uncertainty in the marker's tooltip", () => {
    setup();
    const oxford = screen.getByRole("button", { name: /Oxford/ });
    expect(oxford.querySelector("title")!.textContent).toContain("51.7520, -1.2577 (published, ±");
  });

  it("adds the distance from the centre once there is one", () => {
    setup({ near: { lat: 51.5074, lon: -0.1278, km: 50 } });
    const oxford = screen.getByRole("button", { name: /Oxford/ });
    expect(oxford.querySelector("title")!.textContent).toMatch(/km from centre/);
  });

  it("pins the centre and rings the radius around it", () => {
    const { container } = setup({ near: { lat: 51.5074, lon: -0.1278, km: 50 } });
    expect(container.querySelector(".de-map-ring")).toBeInTheDocument();
    expect(container.querySelectorAll(".de-map-pin")).toHaveLength(2);
  });

  it("draws nothing of a radius filter that is not set", () => {
    const { container } = setup({ near: null });
    expect(container.querySelector(".de-map-ring")).not.toBeInTheDocument();
  });

  it("tells a synthetic node from a real one, once there is one of each", () => {
    const registry = new Map(REGISTRY);
    registry.set("synth-1", {
      ...node("synth-1", "Sim one", 51.6, -0.5),
      synthetic: true,
    });
    const { container } = render(
      <NodeMap
        filters={defaultFilters(TODAY)}
        nodes={registry}
        effective={new Set(registry.keys())}
        radiusKm={DEFAULT_RADIUS_KM}
        onChange={vi.fn()}
      />,
    );
    expect(container.querySelectorAll(".de-map-node-synth")).toHaveLength(1);
  });

  it("leaves every node the same colour when none of them is synthetic", () => {
    const { container } = setup();
    expect(container.querySelectorAll(".de-map-node-synth")).toHaveLength(0);
  });

  it("zooms to the centre when one is placed, so the ring is on screen", () => {
    const { container } = setup({ near: { lat: 51.5074, lon: -0.1278, km: 50 } });
    fireEvent.click(screen.getByRole("button", { name: /Oxford/ }));
    expect(screen.getByRole("button", { name: /around centre/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(container.querySelector(".de-map-ring")).toBeInTheDocument();
  });

  it("offers no centred view while there is no centre to build one around", () => {
    setup({ near: null });
    expect(screen.getByRole("button", { name: /around centre/i })).toBeDisabled();
  });
});
