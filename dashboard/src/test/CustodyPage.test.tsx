import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import CustodyPage from "../pages/admin/CustodyPage";

vi.mock("../api/client", () => ({
  api: {
    custody: vi.fn(),
    adminNodeRefs: vi.fn().mockResolvedValue({}),
  },
}));

const REF = "nde1a2b3c4d00";

describe("Chain of custody: windowed chain", () => {
  it("shows the latest window end, verified against entries, and gaps", async () => {
    (api.custody as any).mockResolvedValue({
      registered_nodes: 1,
      node_keys: { [REF]: { signing_mode: "hardware", fingerprint: "ab:cd" } },
      chain_entries: {
        [REF]: {
          count: 5,
          latest_window_end: "2026-10-19T10:00:00Z",
          latest_verified: true,
          verified: 4,
          gaps: 1,
        },
      },
      iq_commitments: {},
    });

    render(
      <MemoryRouter>
        <CustodyPage />
      </MemoryRouter>,
    );

    const row = (await screen.findByText(REF)).closest("tr") as HTMLElement;
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toContain("Latest window end (UTC)");
    expect(headers).not.toContain("Latest Hour (UTC)");
    const cell = (name: string) => row.children[headers.indexOf(name)];
    expect(cell("Latest window end (UTC)")).toHaveTextContent("2026-10-19T10:00:00Z");
    expect(cell("Verified / Entries")).toHaveTextContent("4 / 5");
    expect(cell("Gaps")).toHaveTextContent("1");
    expect(within(row).getByText("Verified")).toBeInTheDocument();
  });
});
