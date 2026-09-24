import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AvailabilityTimeline } from "../../pages/user/dataExplorer/AvailabilityTimeline";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { defaultFilters, type ExplorerFilters } from "../../pages/user/dataExplorer/urlState";
import { timelineProps } from "../stubs/edscTimeline";

const TODAY = "2026-09-17";
const at = (iso: string) => Date.parse(iso);

const FILES: ArchiveFile[] = [
  {
    key: "year=2026/month=09/day=16/node_ref=ret-a/part-0.parquet",
    name: "part-0.parquet",
    node: "ret-a",
    day: "2026-09-16",
    size: 1000,
    startMs: at("2026-09-16T05:00:00Z"),
    endMs: at("2026-09-16T06:00:00Z"),
  },
];

function setup(over: Partial<ExplorerFilters> = {}, ids = ["ret-a"], anySynthetic = false) {
  const onChange = vi.fn();
  const filters = { ...defaultFilters(TODAY), ...over };
  const props = {
    filters,
    today: TODAY,
    ids,
    files: FILES,
    synthetic: (id: string) => id.startsWith("synth-"),
    anySynthetic,
    onChange,
  };
  const view = render(<AvailabilityTimeline {...props} />);
  return { onChange, filters, props, view };
}

/** The lazy import settles after the first render. */
const timeline = () => screen.findByTestId("edsc-timeline");

describe("AvailabilityTimeline", () => {
  beforeEach(() => {
    timelineProps.last = null;
  });

  it("draws the matching files and highlights the filtered range", async () => {
    setup();
    await timeline();
    expect(timelineProps.last.data).toEqual([
      expect.objectContaining({ title: "ret-a", intervals: [[at("2026-09-16T05:00:00Z"), at("2026-09-16T06:00:00Z")]] }),
    ]);
    expect(timelineProps.last.temporalRange).toEqual({
      start: at("2026-09-15T00:00:00Z"),
      end: at("2026-09-18T00:00:00Z"),
    });
    expect(screen.getByTestId("de-tl-range")).toHaveTextContent("2026-09-15 → 2026-09-17");
  });

  it("a range dragged on the timeline becomes the date filters", async () => {
    const { onChange } = setup();
    await timeline();
    act(() =>
      timelineProps.last.onTemporalSet({
        temporalStart: at("2026-09-10T00:00:00Z"),
        temporalEnd: at("2026-09-12T00:00:00Z"),
        center: at("2026-09-11T00:00:00Z"),
        zoom: 2,
      }),
    );
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ from: "2026-09-10", to: "2026-09-11" }),
    );
  });

  it("a focused hour becomes a time-of-day window", async () => {
    const { onChange } = setup();
    await timeline();
    act(() =>
      timelineProps.last.onFocusedSet({
        focusedStart: at("2026-09-16T05:00:00Z"),
        focusedEnd: at("2026-09-16T06:00:00Z"),
      }),
    );
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ from: "2026-09-16", to: "2026-09-16", todFrom: "05:00", todTo: "06:00" }),
    );
  });

  it("Clear range resets the dates and keeps the other filters", () => {
    const { onChange } = setup({ from: "2026-09-01", todFrom: "06:00", minSize: 4096 });
    fireEvent.click(screen.getByRole("button", { name: "Clear range" }));
    expect(onChange).toHaveBeenCalledWith({ ...defaultFilters(TODAY), minSize: 4096 });
  });

  // Without this, any filter change snaps a panned or zoomed view back to
  // where it started.
  it("hands back the view the timeline last reported", async () => {
    const { props, view } = setup();
    await timeline();
    const initialCenter = timelineProps.last.center;
    expect(initialCenter).toBe((at("2026-09-15T00:00:00Z") + at("2026-09-18T00:00:00Z")) / 2);

    act(() => timelineProps.last.onScrollZoom({ center: at("2026-08-01T00:00:00Z"), zoom: 3 }));
    view.rerender(<AvailabilityTimeline {...props} filters={{ ...props.filters, minSize: 1 }} />);

    expect(timelineProps.last.center).toBe(at("2026-08-01T00:00:00Z"));
    expect(timelineProps.last.zoom).toBe(3);
  });

  it("names synthetic nodes in the legend only when one exists", () => {
    const { view } = setup();
    expect(screen.queryByText("synth node")).toBeNull();
    expect(screen.getByText("node")).toBeInTheDocument();
    view.unmount();

    setup({}, ["ret-a"], true);
    expect(screen.getByText("synth node")).toBeInTheDocument();
    expect(screen.getByText("real node")).toBeInTheDocument();
  });

  it("says how to get one row per node once the rows aggregate", () => {
    const { view } = setup();
    expect(screen.queryByText(/Pick up to 3 nodes/)).toBeNull();
    view.unmount();

    setup({}, ["ret-a", "ret-b", "ret-c", "ret-d"]);
    expect(screen.getByText(/Pick up to 3 nodes/)).toBeInTheDocument();
  });
});
