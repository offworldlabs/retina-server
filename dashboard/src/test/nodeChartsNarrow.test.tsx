import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { OwnedNode, OwnerAnalytics } from "../types";
import OverviewPage from "../pages/user/OverviewPage";
import { NodeFigures } from "../pages/user/nodeDetail/NodeFigures";
import { ownedNode } from "./fixtures/ownedNode";
import { stubViewport } from "./matchMedia";

vi.mock("../api/client", () => ({
  api: { myNodes: vi.fn(), myAnalytics: vi.fn(), myAircraft: vi.fn(), myClaims: vi.fn() },
}));
vi.mock("../context/AuthContext", () => ({ useAuth: () => ({ polledRadarRegistration: false }) }));

// Recharts' parts, each recording the props it was given and drawing its children.
const seen = vi.hoisted(() => ({ props: {} as Record<string, Record<string, unknown>> }));
vi.mock("recharts", () => {
  const part = (name: string) => {
    const Stub = (props: Record<string, unknown>) => {
      const { children, ...rest } = props;
      seen.props[name] = rest;
      return <div data-testid={name}>{children as never}</div>;
    };
    return Stub;
  };
  return {
    ResponsiveContainer: part("ResponsiveContainer"),
    BarChart: part("BarChart"),
    Bar: part("Bar"),
    XAxis: part("XAxis"),
    YAxis: part("YAxis"),
    CartesianGrid: part("CartesianGrid"),
    Tooltip: part("Tooltip"),
  };
});

const REFS = ["a1", "b2", "c3", "d4", "e5", "f6"].map((tail) => `nde0123456789${tail}`);
const SIX = REFS.map((ref, i) => ownedNode({ node_id: `ret${i}a2b3c4d`, node_ref: ref, name: ref }));

function renderOverview(nodes: OwnedNode[]) {
  vi.mocked(api.myNodes).mockResolvedValue(nodes);
  vi.mocked(api.myAnalytics).mockResolvedValue({ nodes: {} } as OwnerAnalytics);
  vi.mocked(api.myAircraft).mockResolvedValue({ aircraft: [] });
  vi.mocked(api.myClaims).mockResolvedValue([]);
  render(<MemoryRouter><OverviewPage /></MemoryRouter>);
  return screen.findByTestId("BarChart");
}

const wrapper = () => document.querySelector<HTMLElement>(".chart-container")!;

describe("the node charts on a phone", () => {
  beforeEach(() => {
    seen.props = {};
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(300);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("slants six node refs on the overview chart so each keeps its label", async () => {
    stubViewport(390);
    await renderOverview(SIX);

    expect(seen.props.XAxis).toMatchObject({ dataKey: "name", interval: 0, angle: -45, textAnchor: "end", height: 86 });
    expect(seen.props.BarChart).toMatchObject({ margin: { top: 5, right: 5, bottom: 0, left: 11 } });
    expect(seen.props.Bar.radius).toEqual([4, 4, 0, 0]);
    expect(wrapper().style.height).toBe("250px");
  });

  it("draws the overview chart exactly as before on a desktop", async () => {
    stubViewport(1280);
    await renderOverview(SIX);

    expect(seen.props.XAxis).toEqual({ dataKey: "name", tick: { fontSize: 11 }, stroke: expect.any(String) });
    expect(seen.props.YAxis).toEqual({ tick: { fontSize: 11 }, stroke: expect.any(String) });
    expect(seen.props.BarChart.layout).toBeUndefined();
    expect(seen.props.BarChart.margin).toBeUndefined();
    expect(seen.props.Bar.radius).toEqual([4, 4, 0, 0]);
    expect(wrapper().hasAttribute("style")).toBe(false);
    expect(wrapper().hasAttribute("data-tooltip-scope")).toBe(false);
  });

  it("keeps the trust chart's 200px and props on a desktop", () => {
    stubViewport(1280);
    render(<NodeFigures analytics={{ metrics: { avg_snr: 12 }, trust: { trust_score: 0.9 }, reputation: { reputation: 0.8 } }} />);

    expect(wrapper().style.height).toBe("200px");
    expect(seen.props.XAxis).toEqual({ dataKey: "name", tick: { fontSize: 11 }, stroke: expect.any(String) });
    expect(seen.props.BarChart.layout).toBeUndefined();
    expect(seen.props.Bar.radius).toEqual([4, 4, 0, 0]);
  });
});
