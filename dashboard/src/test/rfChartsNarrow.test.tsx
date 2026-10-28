import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NetworkContribution } from "../pages/user/contribution/NetworkContribution";
import type { NodeSummary } from "../pages/user/contribution/figures";
import { NetworkSnrCard } from "../pages/user/rf/SnrCards";
import type { NetworkNode } from "../pages/user/rf/feeds";
import { stubViewport } from "./matchMedia";

// Each recharts piece hands its props over, and a chart its children, so a
// test reads what the page asked for without a layout engine.
const seen = vi.hoisted(() => ({ chart: [] as Record<string, unknown>[], bar: [] as Record<string, unknown>[] }));
vi.mock("recharts", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const Chart = (props: Record<string, unknown> & { children?: ReactNode }) => {
    seen.chart.push(props);
    return <div>{props.children}</div>;
  };
  const Bar = (props: Record<string, unknown>) => {
    seen.bar.push(props);
    return null;
  };
  const Nothing = () => null;
  return {
    ResponsiveContainer: Pass,
    BarChart: Chart,
    LineChart: Chart,
    Bar,
    Cell: Nothing,
    XAxis: Nothing,
    YAxis: Nothing,
    CartesianGrid: Nothing,
    Tooltip: Nothing,
    Legend: Nothing,
  };
});

// Twenty nodes with twelve-character names, so a phone's width cannot hold
// the labels under their bars.
const REFS = Array.from({ length: 20 }, (_, i) => `nde${String(i).padStart(12, "0")}`);
const NODES = new Map<string, NodeSummary>(REFS.map((ref, i) => [ref, { metrics: { total_detections: 100 - i } }]));
const NETWORK = REFS.map(
  (ref, i): NetworkNode => ({
    ref,
    name: null,
    frequency: null,
    sampleRate: null,
    location: {},
    summary: { metrics: { avg_snr: 20 - i } },
  }),
);

const lastBar = () => seen.bar[seen.bar.length - 1];
const lastChart = () => seen.chart[seen.chart.length - 1];

function contribution() {
  return render(
    <MemoryRouter>
      <NetworkContribution
        nodes={NODES}
        loading={false}
        overlaps={null}
        standings={null}
        mine={new Set()}
        names={new Map()}
      />
    </MemoryRouter>,
  );
}

describe("charts on a phone", () => {
  beforeEach(() => {
    seen.chart.length = 0;
    seen.bar.length = 0;
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(300);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lays the contribution chart's twenty bars on their side", () => {
    stubViewport(390);
    contribution();

    expect(lastChart().layout).toBe("vertical");
    expect(lastBar().radius).toEqual([0, 4, 4, 0]);
  });

  it("draws the contribution chart upright at desktop width", () => {
    stubViewport(1280);
    contribution();

    expect(lastChart().layout).toBeUndefined();
    expect(lastBar().radius).toEqual([4, 4, 0, 0]);
  });

  it("lays the network SNR chart on its side on a phone and upright on a desktop", () => {
    stubViewport(390);
    const phone = render(<NetworkSnrCard nodes={NETWORK} mine={new Set()} loading={false} failed={false} />);
    expect(lastChart().layout).toBe("vertical");
    expect(lastBar().radius).toEqual([0, 4, 4, 0]);
    phone.unmount();

    stubViewport(1280);
    render(<NetworkSnrCard nodes={NETWORK} mine={new Set()} loading={false} failed={false} />);
    expect(lastChart().layout).toBeUndefined();
    expect(lastBar().radius).toEqual([4, 4, 0, 0]);
  });
});
