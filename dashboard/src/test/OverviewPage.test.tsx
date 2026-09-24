import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import OverviewPage from "../pages/user/OverviewPage";

vi.mock("../api/client", () => ({
  api: { nodes: vi.fn(), analytics: vi.fn(), aircraft: vi.fn(), myNodes: vi.fn() },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

// A private node is on no public listing, so the owner's own copy is the only
// one the page has, and it carries the node_id the page must not show.
const OWNED = {
  node_id: "ret1a2b3c4d",
  node_ref: "nde0123456789",
  name: "nde0123456789",
  status: "active",
  position_status: "missing_rx",
  location_private: true,
};

function renderWith(mine: object[]) {
  vi.mocked(api.nodes).mockResolvedValue({ nodes: {} });
  vi.mocked(api.analytics).mockResolvedValue({ nodes: {} });
  vi.mocked(api.aircraft).mockResolvedValue({ aircraft: [] });
  vi.mocked(api.myNodes).mockResolvedValue(mine);
  render(
    <MemoryRouter>
      <OverviewPage />
    </MemoryRouter>,
  );
}

describe("OverviewPage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("names an owned node by its node_ref", async () => {
    renderWith([OWNED]);
    expect(await screen.findAllByText("nde0123456789")).toHaveLength(2);
    expect(screen.queryByText("ret1a2b3c4d")).not.toBeInTheDocument();
  });

  it("shows a dash, not the node_id, for a node with neither a name nor a ref", async () => {
    renderWith([{ ...OWNED, node_ref: null, name: null }]);
    await screen.findByText("My Nodes");
    expect(screen.queryByText(/ret1a2b3c4d/)).not.toBeInTheDocument();
    expect(screen.getByRole("table").querySelector("td")).toHaveTextContent(/^—$/);
    expect(document.querySelector(".node-name")).toHaveTextContent(/—$/);
  });
});
