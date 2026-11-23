import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { Bar, BarChart, Legend, Tooltip, XAxis, YAxis } from "recharts";
import { chartLayout, useChartLayout, useTapOutTooltip, type ChartFrame } from "../utils/chartLayout";
import { stubViewport } from "./matchMedia";

// The admin trust chart as the desktop draws it: node refs under the bars at
// 9px on a slant, values 0 to 100, Recharts' default legend underneath.
const TRUST: ChartFrame = {
  height: 250,
  xAxis: { dataKey: "name", tick: { fontSize: 9 }, interval: 0, angle: -35, textAnchor: "end", height: 50 },
  yAxis: { tick: { fontSize: 11 }, domain: [0, 100] },
  legend: {},
};

const BELOW = { layout: "horizontal", align: "center", verticalAlign: "bottom" } as const;
const COMPACT = { top: 5, right: 5, bottom: 0, left: 0 };

describe("chartLayout", () => {
  it("hands a desktop chart back the very frame it was given", () => {
    for (const kind of ["bar", "line", "area", "pie"] as const) {
      expect(chartLayout(TRUST, { desktop: true, width: 360, categories: 34, longestLabel: 12, kind })).toBe(TRUST);
    }
    // A wrapper not laid out yet has no width to fit to.
    expect(chartLayout(TRUST, { desktop: false, width: 0, categories: 34, kind: "bar" })).toBe(TRUST);
  });

  it("turns a phone's many bars on their side, a row each", () => {
    const at = { desktop: false, width: 360, inset: 32, categories: 34, longestLabel: 10, kind: "bar" } as const;
    // Bars 295 / 34 = 8.7px wide, where a slanted 9px label needs 19.8.
    expect(chartLayout(TRUST, at)).toEqual({
      layout: "vertical",
      margin: COMPACT,
      // inset 32 + margins 5 + 34 rows of 28 + value axis 30 + legend row 24
      height: 1043,
      xAxis: { type: "number", tick: { fontSize: 11 }, domain: [0, 100] },
      yAxis: { type: "category", dataKey: "name", tick: { fontSize: 9 }, interval: 0, width: 62 },
      legend: BELOW,
    });
    expect(chartLayout(TRUST, { ...at, categories: 20 }).height).toBe(651);
  });

  it("slants fifteen labels at 390 so every bar keeps its own", () => {
    const frame = chartLayout(TRUST, { desktop: false, width: 390, categories: 15, longestLabel: 12, kind: "bar" });
    // A 64.8px label does not fit a 325 / 15 = 21.7px bar flat, but slanted
    // it needs (9 + 5) / sin 45° = 19.8, and drops 8 + (64.8 + 9) sin 45°.
    expect(frame).toEqual({
      margin: COMPACT,
      height: 250,
      xAxis: { dataKey: "name", tick: { fontSize: 9 }, interval: 0, angle: -45, textAnchor: "end", height: 61 },
      yAxis: TRUST.yAxis,
      legend: BELOW,
    });
  });

  it("keeps a tablet's bars upright, thinning the labels when slanting is not enough", () => {
    const at = { desktop: false, width: 820, categories: 15, longestLabel: 12, kind: "bar" } as const;
    const fifteen = chartLayout(TRUST, at);
    expect(fifteen.layout).toBeUndefined();
    expect(fifteen.xAxis).toMatchObject({ interval: 0, angle: -45 });
    // 755 / 45 = 16.8px bars: a label every other bar.
    const many = chartLayout(TRUST, { ...at, categories: 45 });
    expect(many.layout).toBeUndefined();
    expect(many.xAxis).toMatchObject({ interval: 1, angle: -45 });
  });

  it("spaces a phone's time axis by the width of its labels", () => {
    const trend: ChartFrame = {
      height: 250,
      xAxis: { dataKey: "time", tick: { fontSize: 10 } },
      yAxis: { tick: { fontSize: 11 } },
    };
    const frame = chartLayout(trend, { desktop: false, width: 360, categories: 60, longestLabel: 8, kind: "line" });
    expect(frame).toEqual({
      margin: COMPACT,
      height: 250,
      xAxis: { dataKey: "time", tick: { fontSize: 10 }, interval: "preserveStartEnd", minTickGap: 48 },
      yAxis: { tick: { fontSize: 11 } },
      legend: undefined,
    });
  });

  it("moves a pie's legend from its side to underneath, and makes room for it", () => {
    const share: ChartFrame = {
      height: 250,
      xAxis: {},
      yAxis: {},
      legend: { layout: "vertical", align: "right", verticalAlign: "middle", iconSize: 10, wrapperStyle: { fontSize: 11, lineHeight: "18px" } },
    };
    const frame = chartLayout(share, { desktop: false, width: 390, categories: 11, longestLabel: 12, kind: "pie" });
    expect(frame.legend).toEqual({ ...BELOW, iconSize: 10, wrapperStyle: { fontSize: 11, lineHeight: "18px" } });
    // Entries of 10 + 4 + 79.2 + 10px, three to a 380px row: four rows of 18.
    expect(frame.height).toBe(322);
  });

  it("leaves a chart already drawn sideways sideways, capping its label column", () => {
    const types: ChartFrame = {
      layout: "vertical",
      margin: { left: 20 },
      height: 250,
      xAxis: { type: "number", tick: { fontSize: 11 }, allowDecimals: false },
      yAxis: { type: "category", dataKey: "name", tick: { fontSize: 11 }, width: 140 },
    };
    expect(chartLayout(types, { desktop: false, width: 300, categories: 8, longestLabel: 24, kind: "bar" })).toEqual({
      ...types,
      yAxis: { ...types.yAxis, width: 120 },
    });
  });
});

/** jsdom has none. Records each observer so a test can report a resize. */
function stubResizeObserver() {
  const callbacks: (() => void)[] = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        callbacks.push(callback);
      }
      observe() {}
      disconnect() {}
    },
  );
  return {
    created: () => callbacks.length,
    resize: () => act(() => callbacks.forEach((callback) => callback())),
  };
}

function TrustChart({ onFrame }: { onFrame: (frame: ChartFrame) => void }) {
  const { ref, frame } = useChartLayout(TRUST, { kind: "bar", categories: 34, longestLabel: 10 });
  useEffect(() => {
    onFrame(frame);
  }, [frame, onFrame]);
  return (
    <div ref={ref} style={{ height: frame.height }}>
      <BarChart width={360} height={frame.height} data={[]} layout={frame.layout} margin={frame.margin}>
        <XAxis {...frame.xAxis} />
        <YAxis {...frame.yAxis} />
        {frame.legend && <Legend {...frame.legend} />}
        <Bar dataKey="trust" />
      </BarChart>
    </div>
  );
}

const latest = (frames: ChartFrame[]) => frames[frames.length - 1];

describe("useChartLayout", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("draws a desktop chart from its desktop frame however narrow its card, and watches nothing", () => {
    stubViewport(1440);
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(360);
    const observers = stubResizeObserver();
    const frames: ChartFrame[] = [];
    render(<TrustChart onFrame={(frame) => frames.push(frame)} />);
    expect(latest(frames)).toBe(TRUST);
    expect(observers.created()).toBe(0);
  });

  it("fits a phone's chart to its card, and refits it when the card resizes", () => {
    stubViewport(390);
    const width = vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(360);
    const observers = stubResizeObserver();
    const frames: ChartFrame[] = [];
    render(<TrustChart onFrame={(frame) => frames.push(frame)} />);
    // jsdom lays nothing out, so the wrapper has no padding: 5 + 34 × 28 + 30 + 24.
    expect(latest(frames)).toMatchObject({ layout: "vertical", height: 1011 });

    width.mockReturnValue(820);
    observers.resize();
    expect(latest(frames).layout).toBeUndefined();
    expect(latest(frames).xAxis).toMatchObject({ interval: 0, angle: -45 });
  });
});

const ROWS = [
  { name: "a", value: 1 },
  { name: "b", value: 2 },
];

function TrustTooltip() {
  const tip = useTapOutTooltip();
  return (
    <>
      <button type="button">Elsewhere</button>
      <div data-testid="chart" {...tip.wrapper}>
        <BarChart width={300} height={200} data={ROWS} {...tip.chart}>
          <XAxis dataKey="name" />
          <Tooltip {...tip.tooltip} />
          <Bar dataKey="value" />
        </BarChart>
      </div>
      <output>{String(tip.tooltip.active)}</output>
    </>
  );
}

describe("useTapOutTooltip", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("closes a touch screen's tooltip on a tap outside the chart, and leaves it to the next tap inside", () => {
    stubViewport(390, { coarse: true });
    render(<TrustTooltip />);
    const chart = screen.getByTestId("chart");
    const tooltip = screen.getByRole("status");

    fireEvent.pointerDown(chart.querySelector("svg")!);
    expect(tooltip).toHaveTextContent("undefined");

    fireEvent.pointerDown(screen.getByRole("button", { name: "Elsewhere" }));
    expect(tooltip).toHaveTextContent("false");

    // Shut until Recharts has the new tap's position, so the old one does not flash back.
    fireEvent.pointerDown(chart.querySelector("svg")!);
    expect(tooltip).toHaveTextContent("false");
    fireEvent.mouseMove(chart.querySelector(".recharts-wrapper")!);
    expect(tooltip).toHaveTextContent("undefined");
  });

  it("changes nothing for a mouse", () => {
    stubViewport(1440);
    const listen = vi.spyOn(document, "addEventListener");
    const { result } = renderHook(() => useTapOutTooltip());
    expect(result.current).toEqual({ wrapper: {}, chart: {}, tooltip: {} });
    expect(listen).not.toHaveBeenCalledWith("pointerdown", expect.anything(), true);
  });
});
