import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SolveHistoryTable } from "../pages/admin/MlatSolveHistory";
import { stubViewport } from "./matchMedia";

type History = Parameters<typeof SolveHistoryTable>[0]["history"];
type Solve = NonNullable<History["solves"]>[number];

afterEach(() => {
  vi.unstubAllGlobals();
});

const at = { host: "admin.retina.fm", protocol: "https:" };

const solve = (ts_ms: number, gt_hex: string): Solve => ({
  ts_ms,
  n_nodes: 3,
  gt_error_km: 1,
  heading_err_deg: 2,
  rms_delay: 0.4,
  rms_doppler: 1.2,
  gt_hex,
  outcome: "published",
  solver_hex: "mn0123456789",
  lat: 34.85,
  lon: -82.39,
  raw_lat: 34.85,
  raw_lon: -82.39,
  archive_map: "shown",
});

const table = (solves: Solve[]) => (
  <SolveHistoryTable history={{ hex: "mn0123456789", window_minutes: 30, solves }} at={at} />
);

const rowOf = (text: string) => screen.getByText(text).closest("tr") as HTMLElement;

describe("the solve history's phone cards", () => {
  it("keep a solve's More open when a newer solve is added above it", () => {
    stubViewport(390);
    const base = Date.UTC(2026, 9, 18, 22, 14, 7);
    const { rerender } = render(table([solve(base, "aaaaaa"), solve(base - 5_000, "bbbbbb")]));
    fireEvent.click(within(rowOf("bbbbbb")).getByRole("button", { name: "More" }));
    expect(rowOf("bbbbbb")).toHaveAttribute("data-card-open");

    rerender(table([solve(base + 5_000, "cccccc"), solve(base, "aaaaaa"), solve(base - 5_000, "bbbbbb")]));

    expect(rowOf("bbbbbb")).toHaveAttribute("data-card-open");
    expect(rowOf("aaaaaa")).not.toHaveAttribute("data-card-open");
    expect(rowOf("cccccc")).not.toHaveAttribute("data-card-open");
  });

  it("keep a solve's More open when a solve from its millisecond is listed ahead of it", () => {
    stubViewport(390);
    const ts = Date.UTC(2026, 9, 18, 22, 14, 7, 412);
    const regular: Solve = { ...solve(ts, "a1b2c3"), measurement_ts_ms: ts - 1_800, rms_delay: 0.731, gt_error_km: 1.84 };
    // The known lane's solve of the same aircraft in the same millisecond,
    // recorded after one poll and listed first in its millisecond by the next.
    const known: Solve = {
      ...solve(ts, "a1b2c3"),
      measurement_ts_ms: ts - 2_300,
      known_lane: true,
      outcome: "known_truth_match",
      published: true,
      lat: null,
      lon: null,
      raw_lat: 34.8612,
      raw_lon: -82.4107,
      rms_delay: 0.208,
      gt_error_km: 0.62,
    };
    const { rerender } = render(table([regular]));
    fireEvent.click(within(rowOf("0.731 µs")).getByRole("button", { name: "More" }));
    expect(rowOf("0.731 µs")).toHaveAttribute("data-card-open");

    // Each poll works the archive verdict out again, so it may differ.
    rerender(table([known, { ...regular, archive_map: null }]));

    expect(rowOf("0.731 µs")).toHaveAttribute("data-card-open");
    expect(rowOf("0.731 µs")).toHaveTextContent("1.84 km");
    expect(rowOf("0.208 µs")).not.toHaveAttribute("data-card-open");
  });
});
