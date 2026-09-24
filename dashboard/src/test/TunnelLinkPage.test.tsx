import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import TunnelLinkPage from "../pages/user/TunnelLinkPage";

vi.mock("../api/client", () => ({ api: { myNodes: vi.fn() } }));

// The owner's own list, which is the one feed that carries node_id: the id
// the node's site is named after.
const NODE = {
  node_id: "ret1a2b3c4d",
  node_ref: "nde0123456789",
  name: "Rooftop",
  status: "active",
  is_synthetic: false,
};

describe("TunnelLinkPage", () => {
  beforeEach(() => {
    vi.mocked(api.myNodes).mockReset();
  });

  it("links each node to the site its node_id names", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    render(<TunnelLinkPage />);
    expect(await screen.findByRole("link", { name: /Rooftop/ })).toHaveAttribute(
      "href",
      "https://ret1a2b3c4d.retnode.com",
    );
  });

  it("offers no link for a synthetic node, which has no site", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, is_synthetic: true }]);
    render(<TunnelLinkPage />);
    expect(await screen.findByText("Rooftop")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Rooftop/ })).toBeNull();
  });

  it("labels a node with no name by its node_ref, never the id its site is named after", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...NODE, name: null }]);
    render(<TunnelLinkPage />);
    const link = await screen.findByRole("link", { name: /nde0123456789/ });
    expect(link).toHaveAttribute("href", "https://ret1a2b3c4d.retnode.com");
    expect(screen.queryByText("ret1a2b3c4d")).not.toBeInTheDocument();
  });

  it("finds a node by its node_ref", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE, { ...NODE, node_id: "ret9f8e7d6c", node_ref: "nde9876543210", name: "Barn" }]);
    render(<TunnelLinkPage />);
    fireEvent.change(await screen.findByPlaceholderText("Search nodes…"), { target: { value: "nde98" } });
    expect(screen.getByText("Barn")).toBeInTheDocument();
    expect(screen.queryByText("Rooftop")).not.toBeInTheDocument();
  });
});
