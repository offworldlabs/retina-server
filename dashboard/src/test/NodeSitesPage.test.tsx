import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { OwnedNode } from "../types";
import NodeSitesPage from "../pages/user/NodeSitesPage";

vi.mock("../api/client", () => ({ api: { myNodes: vi.fn() } }));

// The owner's own list, which is the one feed that carries node_id: the id
// the node's site is named after.
const NODE = {
  node_id: "ret1a2b3c4d",
  node_ref: "nde0123456789",
  name: "Rooftop",
  status: "active",
  is_synthetic: false,
} as OwnedNode;

describe("NodeSitesPage", () => {
  beforeEach(() => {
    vi.mocked(api.myNodes).mockReset();
  });

  it("is named for what it does, with none of the tunnel controls it once faked", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE, { ...NODE, node_id: "ret9f8e7d6c", node_ref: "nde9876543210", name: "Barn", status: "offline" }]);
    render(<NodeSitesPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Node Sites" })).toBeInTheDocument();
    expect(screen.getByText("Rooftop")).toBeInTheDocument();
    expect(screen.queryByText(/Enable Tunnel/i)).toBeNull();
    expect(screen.queryByText(/Not Yet Available/i)).toBeNull();
    expect(screen.queryByText(/Coming Soon/i)).toBeNull();
    expect(screen.queryByTitle(/Coming soon/i)).toBeNull();
    expect(screen.queryByText(/\[node-ip\]/)).toBeNull();
    for (const header of ["Local Display", "Tunnel Status", "Actions"]) {
      expect(screen.queryByRole("columnheader", { name: header })).toBeNull();
    }
    for (const row of screen.getAllByRole("row").slice(1)) {
      expect(within(row).queryByRole("button")).toBeNull();
    }
  });

  it("links each node to the site its node_id names", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    render(<NodeSitesPage />);
    expect(await screen.findByRole("link", { name: /Rooftop/ })).toHaveAttribute(
      "href",
      "https://ret1a2b3c4d.retnode.com",
    );
  });

  it("offers no link for a synthetic node, which has no site", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, is_synthetic: true }]);
    render(<NodeSitesPage />);
    expect(await screen.findByText("Rooftop")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Rooftop/ })).toBeNull();
  });

  it("lists a polled radar without a link, since the server polls it and it serves no site", async () => {
    const radar = {
      ...NODE,
      node_id: "bla5e6f7a8b",
      node_ref: "nde5555555555",
      name: "Stock radar",
      polled: { address: "radar.example.com:3000", unprotected: false, liveness: "streaming", trust_state: "probation" },
    } as OwnedNode;
    vi.mocked(api.myNodes).mockResolvedValue([NODE, radar]);
    const { container } = render(<NodeSitesPage />);

    expect(await screen.findByText("Stock radar")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Stock radar/ })).toBeNull();
    expect(container.querySelector('a[href*="bla"]')).toBeNull();
    expect(screen.getByRole("link", { name: /Rooftop/ })).toHaveAttribute("href", "https://ret1a2b3c4d.retnode.com");
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("labels a node with no name by its node_ref, never the id its site is named after", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, name: null }]);
    render(<NodeSitesPage />);
    const link = await screen.findByRole("link", { name: /nde0123456789/ });
    expect(link).toHaveAttribute("href", "https://ret1a2b3c4d.retnode.com");
    expect(screen.queryByText("ret1a2b3c4d")).not.toBeInTheDocument();
  });

  it("finds a node by its node_ref", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE, { ...NODE, node_id: "ret9f8e7d6c", node_ref: "nde9876543210", name: "Barn" }]);
    render(<NodeSitesPage />);
    fireEvent.change(await screen.findByPlaceholderText("Search nodes…"), { target: { value: "nde98" } });
    expect(screen.getByText("Barn")).toBeInTheDocument();
    expect(screen.queryByText("Rooftop")).not.toBeInTheDocument();
  });

  it("starts a search from the first page, whichever page was open", async () => {
    const many = Array.from({ length: 80 }, (_, i) => ({
      ...NODE,
      node_id: `ret${String(i).padStart(8, "0")}`,
      node_ref: `nde${String(i).padStart(10, "0")}`,
      name: `${i < 30 ? "Alpha" : "Beta"} ${String(i).padStart(2, "0")}`,
    }));
    vi.mocked(api.myNodes).mockResolvedValue(many);
    render(<NodeSitesPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Node Sites" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(screen.getByText(/Page 4 of 4/)).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("Search nodes…"), { target: { value: "beta" } });
    expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();
    expect(screen.getByText("Beta 30")).toBeInTheDocument();
    expect(screen.queryByText("Alpha 00")).not.toBeInTheDocument();
  });
});
