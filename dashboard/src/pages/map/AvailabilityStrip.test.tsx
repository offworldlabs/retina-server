import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { timelineProps } from "../../test/stubs/edscTimeline";
import AvailabilityStrip, { type AvailabilityStripProps } from "./AvailabilityStrip";
import { ARCHIVE_LAG_MS } from "./replay/useArchiveReplay";

const at = (iso: string) => Date.parse(iso);
const NOW = at("2026-10-19T12:00:00Z");
const CLOCK = at("2026-10-18T20:14:05Z");
const WINDOW = { from: at("2026-10-18T19:59:05Z"), to: at("2026-10-18T20:29:05Z") };

function availability(over: Partial<AvailabilityStripProps["availability"]> = {}) {
  return {
    hours: [
      { start_ms: at("2026-10-18T18:00:00Z"), count: 40, unclassified: 0 },
      { start_ms: at("2026-10-18T19:00:00Z"), count: 55, unclassified: 3 },
      { start_ms: at("2026-10-18T20:00:00Z"), count: 0, unclassified: 0 },
      { start_ms: at("2026-10-18T21:00:00Z"), count: 0, unclassified: 8 },
    ],
    status: "ready" as const,
    error: null,
    onViewCenter: vi.fn(),
    ...over,
  };
}

async function setup(over: Partial<AvailabilityStripProps> = {}) {
  const p: AvailabilityStripProps = {
    clockMs: CLOCK,
    windowRange: WINDOW,
    availability: availability(),
    onSeek: vi.fn(),
    ...over,
  };
  const view = render(<AvailabilityStrip {...p} />);
  await screen.findByTestId("edsc-timeline");
  return { p, view };
}

const last = () => {
  if (!timelineProps.last) throw new Error("the timeline was not rendered");
  return timelineProps.last;
};

beforeEach(() => {
  timelineProps.last = null;
  vi.spyOn(Date, "now").mockReturnValue(NOW);
});
afterEach(() => vi.restoreAllMocks());

describe("AvailabilityStrip hands the timeline", () => {
  it("a solves row and an unclassified row, with the empty hour left as a gap", async () => {
    await setup();
    expect(last().data).toEqual([
      {
        id: "solves",
        title: "Solves",
        color: "var(--accent)",
        intervals: [[at("2026-10-18T18:00:00Z"), at("2026-10-18T20:00:00Z")]],
      },
      {
        id: "unclassified",
        title: "Unclassified (fleet unknown)",
        color: "var(--warning)",
        intervals: [
          [at("2026-10-18T19:00:00Z"), at("2026-10-18T20:00:00Z")],
          [at("2026-10-18T21:00:00Z"), at("2026-10-18T22:00:00Z")],
        ],
      },
    ]);
  });

  it("the loaded window as its range, centred on the replay instant at the hour view", async () => {
    await setup();
    expect(last().temporalRange).toEqual({ start: WINDOW.from, end: WINDOW.to });
    expect(last().center).toBe(CLOCK);
    expect(last().zoom).toBe(1);
    expect(last().minZoom).toBe(0);
    expect(last().maxZoom).toBe(2);
  });

  it("no range before a window is loaded", async () => {
    await setup({ windowRange: null });
    expect(last().temporalRange).toEqual({});
  });

  it("its own view back after a pan, rather than the moving clock", async () => {
    const { p, view } = await setup();
    const panned = at("2026-10-15T12:00:00Z");
    act(() => last().onDragPan?.({ center: panned, zoom: 2 }));
    view.rerender(<AvailabilityStrip {...p} clockMs={CLOCK + 5000} />);
    expect(last().center).toBe(panned);
    expect(last().zoom).toBe(2);
  });
});

describe("AvailabilityStrip cold storage", () => {
  const cold = (iso: string) => ({ start_ms: at(iso), count: 20, unclassified: 0, cold: true });

  it("draws hours kept only in cold storage on their own row and names them in the legend", async () => {
    const hours = [...availability().hours, cold("2026-10-18T22:00:00Z")];
    const { view } = await setup({ availability: availability({ hours }) });
    expect(last().data.map((row) => row.id)).toEqual(["solves", "cold", "unclassified"]);
    expect(last().data[0].intervals).toEqual([[at("2026-10-18T18:00:00Z"), at("2026-10-18T20:00:00Z")]]);
    expect(last().data[1]).toEqual({
      id: "cold",
      title: "Cold storage",
      color: "var(--text-secondary)",
      intervals: [[at("2026-10-18T22:00:00Z"), at("2026-10-18T23:00:00Z")]],
    });
    expect(view.container.querySelector(".archive-legend")).toHaveTextContent("Cold storage");
    expect(view.container.querySelector(".archive-swatch.is-cold")).not.toBeNull();
  });

  it("keeps the unclassified mark of a cold hour, on the cold row and not on the solves row", async () => {
    const hours = [{ ...cold("2026-10-18T22:00:00Z"), unclassified: 5 }];
    await setup({ availability: availability({ hours }) });
    expect(last().data).toEqual([
      { id: "solves", title: "Solves", color: "var(--accent)", intervals: [] },
      {
        id: "cold",
        title: "Cold storage",
        color: "var(--text-secondary)",
        intervals: [[at("2026-10-18T22:00:00Z"), at("2026-10-18T23:00:00Z")]],
      },
      {
        id: "unclassified",
        title: "Unclassified (fleet unknown)",
        color: "var(--warning)",
        intervals: [[at("2026-10-18T22:00:00Z"), at("2026-10-18T23:00:00Z")]],
      },
    ]);
  });

  it("has no cold row or legend entry when no hour is cold", async () => {
    const { view } = await setup();
    expect(last().data.map((row) => row.id)).toEqual(["solves", "unclassified"]);
    expect(view.container.querySelector(".archive-legend")).not.toHaveTextContent("Cold storage");
    expect(view.container.querySelector(".archive-swatch.is-cold")).toBeNull();
  });
});

describe("AvailabilityStrip gestures", () => {
  it("seek a dragged range to its middle, to the minute", async () => {
    const { p } = await setup();
    act(() =>
      last().onTemporalSet?.({
        temporalStart: at("2026-10-17T10:00:00Z"),
        temporalEnd: at("2026-10-17T10:41:10Z"),
      }),
    );
    expect(p.onSeek).toHaveBeenCalledWith(at("2026-10-17T10:21:00Z"));
  });

  it("seek a clicked unit to its middle", async () => {
    const { p } = await setup();
    act(() =>
      last().onFocusedSet?.({
        focusedStart: at("2026-10-17T14:00:00Z"),
        focusedEnd: at("2026-10-17T15:00:00Z"),
      }),
    );
    expect(p.onSeek).toHaveBeenCalledWith(at("2026-10-17T14:30:00Z"));
  });

  it("never seek past now less ARCHIVE_LAG_MS", async () => {
    const { p } = await setup();
    act(() =>
      last().onFocusedSet?.({ focusedStart: at("2026-10-19T00:00:00Z"), focusedEnd: at("2026-10-20T00:00:00Z") }),
    );
    expect(p.onSeek).toHaveBeenLastCalledWith(NOW - ARCHIVE_LAG_MS);
    act(() =>
      last().onTemporalSet?.({ temporalStart: at("2026-10-19T11:30:00Z"), temporalEnd: at("2026-10-19T13:30:00Z") }),
    );
    expect(p.onSeek).toHaveBeenLastCalledWith(NOW - ARCHIVE_LAG_MS);
  });

  it("do not seek when the gesture carries no span", async () => {
    const { p } = await setup();
    act(() => last().onFocusedSet?.({ center: CLOCK }));
    act(() => last().onTemporalSet?.({}));
    expect(p.onSeek).not.toHaveBeenCalled();
  });

  it("report where the view settled after a move or a pan", async () => {
    const { p } = await setup();
    const c1 = at("2026-10-16T09:00:00Z");
    const c2 = at("2026-10-12T09:00:00Z");
    act(() => last().onTimelineMoveEnd?.({ center: c1, zoom: 1 }));
    expect(p.availability.onViewCenter).toHaveBeenLastCalledWith(c1, 1);
    for (const cb of ["onButtonPan", "onArrowKeyPan", "onDragPan", "onScrollPan"] as const) {
      act(() => last()[cb]?.({ center: c2, zoom: 1 }));
      expect(p.availability.onViewCenter, cb).toHaveBeenLastCalledWith(c2, 1);
    }
    expect(p.availability.onViewCenter).toHaveBeenCalledTimes(5);
    expect(p.onSeek).not.toHaveBeenCalled();
  });

  it("report a zoom with the zoom it settled on, and keep it to the view", async () => {
    const { p, view } = await setup();
    act(() => last().onScrollZoom?.({ center: CLOCK, zoom: 2 }));
    expect(p.availability.onViewCenter).toHaveBeenLastCalledWith(CLOCK, 2);
    const c = at("2026-10-16T09:00:00Z");
    act(() => last().onButtonZoom?.({ center: c, zoom: 0 }));
    expect(p.availability.onViewCenter).toHaveBeenLastCalledWith(c, 0);
    expect(p.availability.onViewCenter).toHaveBeenCalledTimes(2);
    view.rerender(<AvailabilityStrip {...p} clockMs={CLOCK + 5000} />);
    expect(last().center).toBe(c);
    expect(last().zoom).toBe(0);
    expect(p.onSeek).not.toHaveBeenCalled();
  });

  it("report the view's own centre and zoom for what a change leaves out", async () => {
    const { p } = await setup();
    act(() => last().onButtonZoom?.({ zoom: 2 }));
    expect(p.availability.onViewCenter).toHaveBeenLastCalledWith(CLOCK, 2);
    const c = at("2026-10-16T09:00:00Z");
    act(() => last().onDragPan?.({ center: c }));
    expect(p.availability.onViewCenter).toHaveBeenLastCalledWith(c, 2);
    act(() => last().onScrollZoom?.({}));
    expect(p.availability.onViewCenter).toHaveBeenLastCalledWith(c, 2);
    expect(p.availability.onViewCenter).toHaveBeenCalledTimes(3);
  });
});

describe("AvailabilityStrip text", () => {
  it("names the three things the strip can show", async () => {
    const { view } = await setup();
    const legend = view.container.querySelector(".archive-legend");
    expect(legend).toHaveTextContent("Solves");
    expect(legend).toHaveTextContent("Unclassified (fleet unknown)");
    expect(legend).toHaveTextContent("No data");
  });

  it("says when availability is loading or failed", async () => {
    const { p, view } = await setup({ availability: availability({ status: "loading" }) });
    expect(screen.getByText("Loading availability…")).toBeInTheDocument();
    view.rerender(<AvailabilityStrip {...p} availability={availability({ status: "error", error: "HTTP 503" })} />);
    expect(screen.getByText("Couldn't load availability: HTTP 503")).toHaveClass("archive-strip-state", "is-error");
  });
});

describe("AvailabilityStrip while a rate limit is waited out", () => {
  it("shows the same loading caption as while loading, so the legend's No data is not the answer", async () => {
    const { p, view } = await setup({ availability: availability({ status: "waiting" }) });
    expect(screen.getByText("Loading availability…")).toHaveClass("archive-strip-state");
    expect(screen.getByText("Loading availability…")).not.toHaveClass("is-error");
    view.rerender(<AvailabilityStrip {...p} availability={availability({ status: "ready" })} />);
    expect(screen.queryByText("Loading availability…")).not.toBeInTheDocument();
  });
});

describe("AvailabilityStrip after a seek", () => {
  const HALF = 15 * 60_000;

  it("recentres on the clock when the window moves clear of the last one, at the visitor's zoom", async () => {
    const { p, view } = await setup();
    act(() => last().onDragPan?.({ center: at("2026-10-15T12:00:00Z"), zoom: 2 }));
    const clock = at("2026-10-10T08:30:00Z");
    const far = { from: clock - HALF, to: clock + HALF };
    view.rerender(<AvailabilityStrip {...p} clockMs={clock} windowRange={far} />);
    expect(last().center).toBe(clock);
    expect(last().zoom).toBe(2);
    expect(last().temporalRange).toEqual({ start: far.from, end: far.to });
    expect(p.availability.onViewCenter).toHaveBeenCalledTimes(1);
  });

  it("recentres on a window that starts where the last one ended", async () => {
    const { p, view } = await setup();
    act(() => last().onDragPan?.({ center: at("2026-10-15T12:00:00Z"), zoom: 1 }));
    const next = { from: WINDOW.to, to: WINDOW.to + 2 * HALF };
    view.rerender(<AvailabilityStrip {...p} clockMs={WINDOW.to + HALF} windowRange={next} />);
    expect(last().center).toBe(WINDOW.to + HALF);
  });

  it("remounts the timeline on a far seek, on the clock at the visitor's zoom, and keeps it across an overlapping window", async () => {
    const { p, view } = await setup();
    act(() => last().onDragPan?.({ center: at("2026-10-15T12:00:00Z"), zoom: 2 }));
    const before = screen.getByTestId("edsc-timeline");
    const next = { from: WINDOW.from + 10 * 60_000, to: WINDOW.to + 10 * 60_000 };
    view.rerender(<AvailabilityStrip {...p} clockMs={CLOCK + 10 * 60_000} windowRange={next} />);
    expect(screen.getByTestId("edsc-timeline")).toBe(before);
    const clock = at("2026-10-10T08:30:00Z");
    view.rerender(<AvailabilityStrip {...p} clockMs={clock} windowRange={{ from: clock - HALF, to: clock + HALF }} />);
    const after = screen.getByTestId("edsc-timeline");
    expect(after).not.toBe(before);
    expect(last().center).toBe(clock);
    expect(last().zoom).toBe(2);
    view.rerender(<AvailabilityStrip {...p} clockMs={clock + 5_000} windowRange={{ from: clock - HALF + 5_000, to: clock + HALF + 5_000 }} />);
    expect(screen.getByTestId("edsc-timeline")).toBe(after);
  });

  it("stays where the visitor put it when the next window overlaps the last", async () => {
    const { p, view } = await setup();
    const panned = at("2026-10-15T12:00:00Z");
    act(() => last().onDragPan?.({ center: panned, zoom: 0 }));
    const next = { from: WINDOW.from + 10 * 60_000, to: WINDOW.to + 10 * 60_000 };
    view.rerender(<AvailabilityStrip {...p} clockMs={CLOCK + 10 * 60_000} windowRange={next} />);
    expect(last().center).toBe(panned);
    expect(last().zoom).toBe(0);
    view.rerender(<AvailabilityStrip {...p} clockMs={CLOCK + 10 * 60_000} windowRange={{ ...next }} />);
    expect(last().center).toBe(panned);
  });

  it("stays put for the first window and across a moment without one", async () => {
    const { p, view } = await setup({ windowRange: null });
    const panned = at("2026-10-15T12:00:00Z");
    act(() => last().onDragPan?.({ center: panned, zoom: 1 }));
    view.rerender(<AvailabilityStrip {...p} windowRange={WINDOW} />);
    expect(last().center).toBe(panned);
    view.rerender(<AvailabilityStrip {...p} windowRange={null} />);
    const shifted = { from: WINDOW.from + 60_000, to: WINDOW.to + 60_000 };
    view.rerender(<AvailabilityStrip {...p} windowRange={shifted} />);
    expect(last().center).toBe(panned);
  });

  it("is the visitor's to move again after a recentre", async () => {
    const { p, view } = await setup();
    const clock = at("2026-10-10T08:30:00Z");
    view.rerender(<AvailabilityStrip {...p} clockMs={clock} windowRange={{ from: clock - HALF, to: clock + HALF }} />);
    expect(last().center).toBe(clock);
    const panned = at("2026-10-08T00:00:00Z");
    act(() => last().onDragPan?.({ center: panned, zoom: 2 }));
    view.rerender(<AvailabilityStrip {...p} clockMs={clock + 5_000} windowRange={{ from: clock - HALF, to: clock + HALF }} />);
    expect(last().center).toBe(panned);
    expect(last().zoom).toBe(2);
  });
});
