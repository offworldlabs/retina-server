import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import rules from "../App.css?raw";

vi.mock("../api/client", () => ({
  api: {
    nodeAnalytics: vi.fn(), nodes: vi.fn(), myNodes: vi.fn(),
    adminStorage: vi.fn(), archive: vi.fn(),
  },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

import { api } from "../api/client";
import StoragePage from "../pages/admin/StoragePage";
import NodeDetailPage from "../pages/user/NodeDetailPage";

/* The console's headerless label/value tables share one class, so their
   labels read alike wherever they appear. */

function kvTables(container: HTMLElement): HTMLTableElement[] {
  return [...container.querySelectorAll("table")].filter((t) => !t.querySelector("thead"));
}

function expectMutedLabels(tables: HTMLTableElement[]) {
  expect(tables.length).toBeGreaterThan(0);
  for (const table of tables) {
    expect(table).toHaveClass("kv-table");
    // The class carries the colour; an inline one would override it.
    for (const row of table.querySelectorAll("tr")) {
      expect((row.cells[0] as HTMLElement).style.color).toBe("");
    }
  }
}

describe("key/value tables", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("mutes the label column in the stylesheet", () => {
    const start = rules.indexOf(".kv-table td:first-child {");
    expect(start).toBeGreaterThanOrEqual(0);
    const body = rules.slice(start, rules.indexOf("}", start));
    expect(body).toMatch(/color:\s*var\(--text-muted\)/);
  });

  it("carry the class on every node detail table", async () => {
    vi.mocked(api.nodeAnalytics).mockResolvedValue({
      node_ref: "a",
      trust: { trust_score: 0.9 },
      reputation: { reputation: 0.8 },
      metrics: { uptime_s: 60, gap_stats: { avg_gap: 1 } },
      detection_area: { estimated_range_km: 40, beam_width_deg: 30 },
    });
    vi.mocked(api.nodes).mockResolvedValue({ nodes: { a: { frequency: 195e6, location: {} } } });
    vi.mocked(api.myNodes).mockResolvedValue([]);
    const { container } = render(
      <MemoryRouter initialEntries={["/nodes/a"]}>
        <Routes><Route path="/nodes/:nodeId" element={<NodeDetailPage />} /></Routes>
      </MemoryRouter>,
    );
    await screen.findByText("Center Frequency");
    const tables = kvTables(container);
    // Trust, timing, detection area and RF configuration.
    expect(tables).toHaveLength(4);
    expectMutedLabels(tables);
  });

  it("carry the class on every storage summary table", async () => {
    vi.mocked(api.adminStorage).mockResolvedValue({
      archive_files: 3, archive_mb: 1, archive_bytes: 1048576,
      disk: { total_gb: 100, used_gb: 50, free_gb: 50, used_pct: 50 },
      write_rate: { total_mb_per_day: 1, days_until_full: 400 },
    });
    vi.mocked(api.archive).mockResolvedValue({ files: [], total: 0 });
    const { container } = render(<StoragePage />);
    await screen.findByText("Total Write Rate");
    const tables = kvTables(container);
    // Disk, write rate and local storage.
    expect(tables).toHaveLength(3);
    expectMutedLabels(tables);
  });
});
