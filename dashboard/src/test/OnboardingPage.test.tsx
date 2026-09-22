import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import OnboardingPage from "../pages/user/OnboardingPage";
import { api } from "../api/client";

// The page is "My Nodes": the owned-node table and how to add one. It reads
// one route, so the mock is the whole of what it may call.
vi.mock("../api/client", () => ({
  api: { myNodes: vi.fn() },
}));

const NODE = {
  node_id: "ret1a2b3c4d",
  node_ref: "nde0123456789",
  name: "Rooftop",
  status: "active",
  last_heartbeat: null,
  is_synthetic: false,
  rx_lat: 51.5,
  rx_lon: -0.12,
  frequency: 195_000_000,
  location_private: false,
  location_privacy_source: "registration",
};

// The Node ref cell of the row for `nodeId`, found by its header so the
// assertion follows the column if the table is reordered.
async function refCell(nodeId: string) {
  const row = (await screen.findByText(nodeId)).closest("tr")!;
  const column = screen.getAllByRole("columnheader").findIndex((h) => h.textContent === "Node ref");
  return row.children[column];
}

describe("OnboardingPage", () => {
  beforeEach(() => {
    vi.mocked(api.myNodes).mockReset();
  });

  it("lists the nodes the caller owns", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    render(<OnboardingPage />);

    expect(await screen.findByText("ret1a2b3c4d")).toBeInTheDocument();
    expect(screen.getByText("195.000 MHz")).toBeInTheDocument();
  });

  it("shows each node under its node_ref, not its name", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    render(<OnboardingPage />);

    expect(await screen.findByText("nde0123456789")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Node ref" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Name" })).not.toBeInTheDocument();
    expect(screen.queryByText("Rooftop")).not.toBeInTheDocument();
  });

  it("marks a node with no published handle rather than leaving the cell blank", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, node_ref: null }]);
    render(<OnboardingPage />);

    expect(await refCell("ret1a2b3c4d")).toHaveTextContent(/^—/);
  });

  it("keeps the location privacy badge beside the node_ref", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, location_private: true }]);
    render(<OnboardingPage />);

    expect(await refCell("ret1a2b3c4d")).toHaveTextContent("nde0123456789 Private");
  });

  it("tells an owner of nothing how a node joins their account", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([]);
    render(<OnboardingPage />);

    expect(await screen.findByText(/click the link we mail you/)).toBeInTheDocument();
  });
});
