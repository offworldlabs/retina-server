import { cloneElement, isValidElement, type ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OwnedSnrCard } from "../pages/user/rf/SnrCards";
import type { KeyedPoll } from "../pages/user/rf/useKeyedPoll";
import type { SnrSeries } from "../types";

// The real chart, at a fixed size: jsdom lays nothing out, so the container
// that measures its parent would hand the chart nothing to draw in. A Line
// draws its dots only once its entry animation ends, which jsdom never sees
// through, so the animation is off.
vi.mock("recharts", async (importOriginal) => {
  const recharts = await importOriginal<typeof import("recharts")>();
  recharts.Line.defaultProps = { ...recharts.Line.defaultProps, isAnimationActive: false };
  return {
    ...recharts,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement<{ width?: number; height?: number }>(children)
        ? cloneElement(children, { width: 640, height: 300 })
        : null,
  };
});

const STEP = 300_000;
const NOW = Math.floor(new Date("2026-10-13T12:00:00Z").getTime() / STEP) * STEP;
const T0 = NOW - 10 * STEP;

// Two buckets that join up, a gap, then one bucket on its own.
const SERIES: SnrSeries = {
  hours: 24,
  bucket_s: 300,
  through_ms: T0 + 5 * STEP,
  points: [
    { t_ms: T0, median_db: 10, p90_db: 14, count: 12 },
    { t_ms: T0 + STEP, median_db: 11, p90_db: 15, count: 9 },
    { t_ms: T0 + 5 * STEP, median_db: 12.5, p90_db: 16.25, count: 20 },
  ],
};

function answered(value: SnrSeries): KeyedPoll<SnrSeries> {
  return { answer: { key: "k", value, at: NOW }, loading: false, error: null, updatedAt: new Date(NOW), refresh: () => {} };
}

describe("an owned node's SNR chart, drawn by Recharts", () => {
  it("draws both lines, broken at the gap, and dots the bucket with no neighbour", () => {
    const { container } = render(<OwnedSnrCard poll={answered(SERIES)} hours={24} bucketS={300} />);

    expect(container.querySelectorAll(".recharts-line")).toHaveLength(2);
    // One dot per line, on the lone bucket; the joined pair is the line's.
    expect(container.querySelectorAll(".recharts-line-dots circle")).toHaveLength(2);
    // The p90 line is the dashed one, so the two part without colour.
    const curves = [...container.querySelectorAll(".recharts-line-curve")];
    expect(curves.map((c) => c.getAttribute("stroke-dasharray"))).toEqual([null, "5 3"]);
    // Each path starts over after the gap rather than drawing through it.
    for (const curve of curves) expect(curve.getAttribute("d")?.match(/M/g)).toHaveLength(2);
    expect(screen.getByText("Median (dB)")).toBeInTheDocument();
    expect(screen.getByText("90th percentile (dB)")).toBeInTheDocument();
  });
});
