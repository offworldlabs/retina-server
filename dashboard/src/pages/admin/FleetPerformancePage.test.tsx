import { cloneElement, isValidElement, type ReactNode } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: ({ children }: { children: ReactNode }) =>
    isValidElement<{ width?: number; height?: number }>(children) ? cloneElement(children, { width: 400, height: 400 }) : null,
}));
vi.mock("../../api/client", () => ({ api: { adminFleetPerformance: vi.fn(), adminNodePerformance: vi.fn(), adminNodeRefs: vi.fn() } }));

import { api } from "../../api/client";
import { stubViewport } from "../../test/matchMedia";
import type { AdminPerformanceRow, NodePerformance } from "../../types";
import FleetPerformancePage from "./FleetPerformancePage";

const row = (node_id: string, over: Partial<AdminPerformanceRow>): AdminPerformanceRow => ({
  node_id,
  rank: null,
  rank_status: null,
  system_constant_db: null,
  system_constant_sigma_db: null,
  system_constant_bound: null,
  threshold_db: null,
  pd_ceiling: null,
  tag_delay_residual_mad_us: null,
  delay_residual_source: null,
  opportunities: null,
  detections: null,
  window: ["2026-10-28", "2026-11-03"],
  tower: null,
  ...over,
});

const NODES = [
  row("node-0001", {
    rank: 2,
    system_constant_db: 31.4,
    system_constant_sigma_db: 1.2,
    threshold_db: 12.34,
    tag_delay_residual_mad_us: 0.42,
    opportunities: 1500,
    tower: { callsign: "EXAMPLE-TV", eirp_dbm: null, status: "matched" },
  }),
  row("node-0002", { rank: 1, system_constant_db: 35, tag_delay_residual_mad_us: 0.3 }),
  row("node-0003", { rank_status: "low_evidence" }),
  row("node-0004", { rank_status: "config_suspect", tag_delay_residual_mad_us: 3.13 }),
];

const open = async () => {
  render(
    <MemoryRouter>
      <FleetPerformancePage />
    </MemoryRouter>,
  );
  await screen.findByText("node-0001");
};

describe("FleetPerformancePage", () => {
  beforeEach(() => {
    vi.mocked(api.adminFleetPerformance).mockResolvedValue({ nodes: NODES });
    vi.mocked(api.adminNodeRefs).mockResolvedValue({ example0001: "node-0001" });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists nodes in the order served, flagged last, with each reason and figure", async () => {
    stubViewport(1280);
    await open();

    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((tr) => tr.querySelector("td")?.textContent)).toEqual(["node-0001", "node-0002", "node-0003", "node-0004"]);
    expect(screen.getByText("2026-10-28 to 2026-11-03")).toBeTruthy();
    expect(within(rows[0]).getByRole("link").getAttribute("href")).toBe("/nodes/example0001");
    expect(within(rows[0]).getByText("Matched (EXAMPLE-TV)")).toBeTruthy();
    expect(within(rows[1]).getByText("35.0 dB")).toBeTruthy();
    expect(rows[1].textContent).not.toContain("±");
    expect(within(rows[2]).getByText("Not enough solid evidence to rank it fairly yet: few samples, no clear detection curve, or no tags or claims to check its setup")).toBeTruthy();
    expect(rows[3].className).toBe("fleet-performance-flagged");
    expect(within(rows[3]).getByText("3.13 µs")).toBeTruthy();
    expect(within(rows[3]).getByText(/need checking/)).toBeTruthy();
  });

  it("shows the ceiling, reads a ceiling nobody saw as not seen, marks a residual read from claims, and words the low-ceiling reason", async () => {
    stubViewport(1280);
    vi.mocked(api.adminFleetPerformance).mockResolvedValue({
      nodes: [
        row("node-0001", { pd_ceiling: 0.91, tag_delay_residual_mad_us: 0.42, delay_residual_source: "claims" }),
        row("node-0005", { rank_status: "low_ceiling", system_constant_db: 20 }),
        row("node-0006", { pd_ceiling: 1 }),
      ],
    });
    await open();

    const [claims, low, unseen] = screen.getAllByRole("row").slice(1);
    expect(claims.querySelector('td[data-label="Ceiling"]')).toHaveTextContent("91%");
    expect(unseen.querySelector('td[data-label="Ceiling"]')).toHaveTextContent("Not seen");
    expect(claims.querySelector('td[data-label="Residual MAD"]')).toHaveTextContent("0.42 µs (claims)");
    expect(low.querySelector('td[data-label="Ceiling"]')).toHaveTextContent("—");
    expect(within(low).getByText("It misses more than half of even its easiest aircraft, so its sensitivity cannot rank it fairly")).toBeTruthy();
  });

  it("keeps the reason and the residual in view on a phone's cards", async () => {
    stubViewport(390);
    await open();

    expect(document.querySelector(".table-wrapper")?.getAttribute("data-phone")).toBe("cards");
    const cells = screen.getAllByRole("row")[4].querySelectorAll("td");
    expect(cells[5].getAttribute("data-label")).toBe("Residual MAD");
    expect(cells[5].getAttribute("data-hint")).toBeNull();
    expect(cells[8].getAttribute("data-label")).toBe("Reason");
    expect(cells[8].getAttribute("data-hint")).toBeNull();
  });

  it("opens a node's full performance from its row, and says when the read fails", async () => {
    stubViewport(1280);
    const performance: NodePerformance = {
      schema: 1, window: ["2026-10-28", "2026-11-03"], opportunities: 900, detections: 500, detections_claims: 300,
      detections_tags: 200, denominator: "aggregator", numerator: "claims", system_constant_db: 31.4,
      system_constant_sigma_db: 1.2, system_constant_bound: "measured", threshold_db: 12.3, pd_ceiling: 0.9, pd_by_class_range_alt: {},
      evidence_by_class_range_alt: {}, pd_by_bearing: Array.from({ length: 72 }, () => 0.5),
      tag_delay_residual_mad_us: 0.4, delay_residual_source: "tags", range_50pct_km: {}, range_50pct_bound: {}, classes_with_evidence: [],
      tower: { callsign: "EXAMPLE-TV", eirp_dbm: null, status: "matched" }, rank_status: null, rank: 2,
    };
    vi.mocked(api.adminNodePerformance).mockResolvedValueOnce(performance).mockRejectedValueOnce(new Error("Server error"));
    await open();

    fireEvent.click(screen.getByRole("button", { name: "Show what node-0001 detects" }));
    expect(await screen.findByRole("heading", { name: "Detection by bearing" })).toBeTruthy();
    expect(api.adminNodePerformance).toHaveBeenCalledWith("node-0001");
    expect(screen.getByRole("heading", { name: "What this node detects" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Show what node-0002 detects" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Could not load this node's performance: Server error");
  });
});
