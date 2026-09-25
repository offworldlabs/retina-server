import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import OnboardingPage from "../pages/user/OnboardingPage";
import { api } from "../api/client";

// The page is "My Nodes": the owned-node table and how to add one. It reads
// one route, so the mock is the whole of what it may call.
vi.mock("../api/client", () => ({
  api: { myNodes: vi.fn() },
}));

const state = vi.hoisted(() => ({ auth: { polledRadarRegistration: false } }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

function renderPage() {
  render(
    <MemoryRouter>
      <OnboardingPage />
    </MemoryRouter>,
  );
}

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
  polled: null,
};

// The Node ref cell of the node's row, found by its header so the assertion
// follows the column if the table is reordered. The row is found by its
// frequency, the one cell every fixture here fills the same way.
async function refCell() {
  const row = (await screen.findByText("195.000 MHz")).closest("tr")!;
  const column = screen.getAllByRole("columnheader").findIndex((h) => h.textContent === "Node ref");
  return row.children[column] as HTMLElement;
}

describe("OnboardingPage", () => {
  beforeEach(() => {
    vi.mocked(api.myNodes).mockReset();
    state.auth = { polledRadarRegistration: false };
  });

  it("lists the nodes the caller owns", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    renderPage();

    expect(await screen.findByText("nde0123456789")).toBeInTheDocument();
    expect(screen.getByText("195.000 MHz")).toBeInTheDocument();
  });

  it("never shows the node_id, which the route carries only for the owner's own calls", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    renderPage();

    await screen.findByText("nde0123456789");
    expect(screen.queryByText("ret1a2b3c4d")).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Node ID" })).not.toBeInTheDocument();
  });

  it("shows each node under its node_ref, not its name", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    renderPage();

    expect(await screen.findByText("nde0123456789")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Node ref" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Name" })).not.toBeInTheDocument();
    expect(screen.queryByText("Rooftop")).not.toBeInTheDocument();
  });

  it("marks a node with no published handle rather than leaving the cell blank", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, node_ref: null }]);
    renderPage();

    expect(await refCell()).toHaveTextContent(/^—/);
  });

  it("links each node to its own page by its node_ref", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    renderPage();

    expect(within(await refCell()).getByRole("link", { name: "nde0123456789" })).toHaveAttribute(
      "href",
      "/nodes/nde0123456789",
    );
  });

  it("links a polled radar to the page that manages it", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([
      {
        ...NODE,
        polled: { address: "radar.example.com:3000", unprotected: false, liveness: "streaming", trust_state: "probation" },
      },
    ]);
    renderPage();

    expect(within(await refCell()).getByRole("link", { name: "nde0123456789" })).toHaveAttribute(
      "href",
      "/radars/nde0123456789",
    );
  });

  it("keeps the location privacy badge beside the node_ref", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, location_private: true }]);
    renderPage();

    expect(await refCell()).toHaveTextContent("nde0123456789 Private");
  });

  it("tells an owner of nothing how a node joins their account", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([]);
    renderPage();

    expect(await screen.findByText(/click the link we mail you/)).toBeInTheDocument();
  });

  it("offers to add a stock blah2 radar where registration is open", async () => {
    state.auth = { polledRadarRegistration: true };
    vi.mocked(api.myNodes).mockResolvedValue([]);
    renderPage();

    expect(await screen.findByRole("link", { name: "Add a stock blah2 radar" })).toHaveAttribute("href", "/radars/new");
  });

  it("offers nothing of the kind where it is not", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([]);
    renderPage();

    await screen.findByText(/click the link we mail you/);
    expect(screen.queryByRole("link", { name: "Add a stock blah2 radar" })).toBeNull();
  });
});
