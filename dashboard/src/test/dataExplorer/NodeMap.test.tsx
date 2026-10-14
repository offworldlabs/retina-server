import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { fitExtent, project, unproject } from "../../pages/user/dataExplorer/mapProjection";
import { NodeMap } from "../../pages/user/dataExplorer/NodeMap";
import type { Positions, RegistryNode } from "../../pages/user/dataExplorer/nodes";
import {
  DEFAULT_RADIUS_KM,
  defaultFilters,
  type ExplorerFilters,
  type NearNode,
  type NearPoint,
} from "../../pages/user/dataExplorer/urlState";
import { distanceKm } from "../../utils/geo";

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

function setup(over: Partial<ExplorerFilters> = {}, effective?: Set<string>, positions?: Positions) {
  const onChange = vi.fn();
  const filters = { ...defaultFilters(TODAY), ...over };
  const { container } = render(
    <NodeMap
      filters={filters}
      nodes={REGISTRY}
      effective={effective ?? new Set(REGISTRY.keys())}
      radiusKm={DEFAULT_RADIUS_KM}
      onChange={onChange}
      positions={positions}
    />,
  );
  return { onChange, container };
}

/** The centre the map last set: a point, or one of an owner's nodes by id. */
const nearFrom = (onChange: ReturnType<typeof vi.fn>): (Partial<NearPoint> & Partial<NearNode>) | null => {
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

describe("NodeMap, on an owner's own nodes", () => {
  // Where the London marker sits in the fitted frame, in the box's own units,
  // which `sized` makes the click's.
  const london = () => project(51.5074, -0.1278, fitExtent(Array.from(REGISTRY.values()))!);

  it("centres on a node by naming it, and puts nothing of where it is in the filter", () => {
    const { onChange } = setup({ near: { lat: 0, lon: 0, km: 25 } }, undefined, "exact");
    fireEvent.click(screen.getByRole("button", { name: /Oxford/ }));
    expect(nearFrom(onChange)).toEqual({ node: "ret-oxford", km: 25 });
  });

  it("names the node from the keyboard as well", () => {
    const { onChange } = setup({ near: null }, undefined, "exact");
    fireEvent.keyDown(screen.getByRole("button", { name: /London/ }), { key: "Enter" });
    expect(nearFrom(onChange)).toEqual({ node: "ret-london", km: DEFAULT_RADIUS_KM });
  });

  it("takes a click that all but lands on a node as one on the node", () => {
    const { onChange, container } = setup({ near: null }, undefined, "exact");
    const svg = container.querySelector("svg")!;
    sized(svg);
    const { x, y } = london();
    fireEvent.click(svg, { clientX: x + 6, clientY: y - 5 });
    expect(nearFrom(onChange)).toEqual({ node: "ret-london", km: DEFAULT_RADIUS_KM });
  });

  it("places a point for a click well clear of every node", () => {
    const { onChange, container } = setup({ near: null }, undefined, "exact");
    const svg = container.querySelector("svg")!;
    sized(svg);
    const { x, y } = london();
    fireEvent.click(svg, { clientX: x + 40, clientY: y });
    expect(nearFrom(onChange)).toMatchObject({ km: DEFAULT_RADIUS_KM });
    expect(nearFrom(onChange)).not.toHaveProperty("node");
  });

  it("leaves a published map's near miss as the point clicked", () => {
    const { onChange, container } = setup({ near: null });
    const svg = container.querySelector("svg")!;
    sized(svg);
    const { x, y } = london();
    fireEvent.click(svg, { clientX: x + 6, clientY: y - 5 });
    const clicked = unproject(x + 6, y - 5, fitExtent(Array.from(REGISTRY.values()))!);
    expect(nearFrom(onChange)).toEqual({ lat: clicked.lat, lon: clicked.lon, km: DEFAULT_RADIUS_KM });
  });

  it("draws a centre on a node where the node is, and measures from it", () => {
    const { container } = setup({ near: { node: "ret-london", km: 50 } }, undefined, "exact");
    expect(container.querySelector(".de-map-ring")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /around centre/i })).toBeEnabled();
    const oxford = screen.getByRole("button", { name: /Oxford/ });
    expect(oxford.querySelector("title")!.textContent).toMatch(/\b(7\d|8\d)\.\d km from centre/);
  });

  it("places a point no nearer a node than 3 km, even zoomed in as far as the map goes", () => {
    // Zoomed to the smallest radius there is, and centred on London, which then
    // sits in the middle of the box. A click just past the snap is a point.
    const { onChange, container } = setup({ near: { node: "ret-london", km: 2 } }, undefined, "exact");
    fireEvent.click(screen.getByRole("button", { name: /around centre/i }));
    const svg = container.querySelector("svg")!;
    sized(svg);

    fireEvent.click(svg, { clientX: 450 + 11, clientY: 150 });
    expect(nearFrom(onChange)).toEqual({ node: "ret-london", km: 2 });

    fireEvent.click(svg, { clientX: 450 + 13, clientY: 150 });
    const point = nearFrom(onChange)!;
    expect(point.node).toBeUndefined();
    expect(distanceKm(point.lat!, point.lon!, 51.5074, -0.1278)).toBeGreaterThan(3);
  });

  it("draws nothing for a centre on a node it cannot place", () => {
    const { container } = setup({ near: { node: "ret-nowhere", km: 50 } }, undefined, "exact");
    expect(container.querySelector(".de-map-ring")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /around centre/i })).toBeDisabled();
  });
});
