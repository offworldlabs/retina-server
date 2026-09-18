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

describe("OnboardingPage", () => {
  beforeEach(() => {
    vi.mocked(api.myNodes).mockReset();
  });

  it("lists the nodes the caller owns", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([NODE]);
    render(<OnboardingPage />);

    expect(await screen.findByText("ret1a2b3c4d")).toBeInTheDocument();
    expect(screen.getByText("195.00 MHz")).toBeInTheDocument();
  });

  it("tells an owner of nothing how a node joins their account", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([]);
    render(<OnboardingPage />);

    expect(await screen.findByText(/click the link we mail you/)).toBeInTheDocument();
  });
});
