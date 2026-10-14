import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CLAIM_PATH } from "../components/ClaimPath";
import ContributionPage from "../pages/user/ContributionPage";
import type { Leaderboard, LeaderboardRow, OwnerAnalytics, OwnerSummary } from "../types";
import { ownedNode } from "./fixtures/ownedNode";

vi.mock("../api/client", () => ({
  api: { analytics: vi.fn(), overlaps: vi.fn(), leaderboard: vi.fn(), myNodes: vi.fn(), myAnalytics: vi.fn() },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

type Auth = { user: { id: number; email: string; name: string } | null; loading: boolean };
const state = vi.hoisted(() => ({ auth: { user: null, loading: false } as Auth, admin: false }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
vi.mock("../utils/surface", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../utils/surface")>()),
  isAdminHost: () => state.admin,
}));

const ADA = { id: 1, email: "a@example.com", name: "Ada" };

// The owner's two nodes: Roof registered private, Mast public.
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
const HILL = "ndehill00000001";
const BARE = "ndebare00000001";

const summary = (ref: string, detections: number, trust = 0.9): OwnerSummary => ({
  node_id: `id-${ref}`,
  node_ref: ref,
  trust: { trust_score: trust },
  detection_area: {},
  metrics: { total_detections: detections, availability_7d: 0.95 },
  reputation: {},
  coverage_map: null,
  empirical_coverage: null,
  node_bias: null,
});

// The public map carries the owner's private node back under its ref with a
// figure the owner's own map does not agree with (40 against 50), so a node
// counted from both would show in the total.
const PUBLIC = {
  nodes: {
    [MAST.node_ref]: summary(MAST.node_ref, 100),
    [HILL]: summary(HILL, 300),
    [BARE]: summary(BARE, 20),
    [ROOF.node_ref]: summary(ROOF.node_ref, 40),
  },
};
const OWN: OwnerAnalytics = {
  nodes: { [MAST.node_ref]: summary(MAST.node_ref, 100), [ROOF.node_ref]: summary(ROOF.node_ref, 50) },
};
// 100 + 300 + 20 + 50, each node once; the owner's 150 of it.
const NETWORK_TOTAL = "470";
const SHARE = "31.9%";

const row = (node_ref: string, name: string, rank: number, detections: number): LeaderboardRow => ({
  node_ref,
  name,
  rank,
  detections,
  frames: 0,
  tracks: 0,
  availability_7d: 0.9,
  avg_snr: 10,
  trust_score: 0.9,
  reputation: 0,
  online: true,
});
const BOARD: Leaderboard = {
  leaderboard: [row(HILL, "Hilltop", 1, 300), row(MAST.node_ref, "mast-node", 2, 100), row(BARE, BARE, 3, 20)],
  total: 3,
};
const OVERLAPS = {
  overlaps: [{ node_a: MAST.node_ref, node_b: HILL, grid_points: 120, has_overlap: true }],
  registered_nodes: [MAST.node_ref, HILL, BARE],
};

function answer({ mine = [ROOF, MAST] } = {}) {
  vi.mocked(api.analytics).mockResolvedValue(PUBLIC);
  vi.mocked(api.overlaps).mockResolvedValue(OVERLAPS);
  vi.mocked(api.leaderboard).mockResolvedValue(BOARD);
  vi.mocked(api.myNodes).mockResolvedValue(mine);
  vi.mocked(api.myAnalytics).mockResolvedValue(OWN);
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ContributionPage />
    </MemoryRouter>,
  );
}

const card = (title: string) => screen.getByRole("heading", { name: title }).closest(".card") as HTMLElement;
const tile = (label: string) => screen.getByText(label).closest(".stat-card") as HTMLElement;
const alertWith = (text: string) =>
  screen.getAllByRole("alert").find((n) => n.textContent?.includes(text)) as HTMLElement | undefined;

describe("ContributionPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    state.auth = { user: ADA, loading: false };
    state.admin = false;
    // usePolling logs every failure.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("is headed Contribution, whoever is looking", async () => {
    answer();
    const owner = renderPage();
    expect(screen.getByRole("heading", { level: 1, name: "Contribution" })).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Your nodes" });
    owner.unmount();

    state.auth = { user: null, loading: false };
    renderPage();
    expect(screen.getByRole("heading", { level: 1, name: "Contribution" })).toBeInTheDocument();
    await screen.findByText("Network Detections");
  });

  it("shows an owner each of their nodes' true figures, a private one included", async () => {
    answer();
    renderPage();

    const nodes = await screen.findByRole("heading", { name: "Your nodes" });
    const table = nodes.closest(".card") as HTMLElement;
    const roof = within(table).getByRole("row", { name: /Roof/ });
    const mast = within(table).getByRole("row", { name: /Mast/ });

    // Roof's 50 is the owner's own figure, not the 40 the public map has.
    expect(within(roof).getByRole("cell", { name: "50" })).toBeInTheDocument();
    expect(within(roof).getByText("Private")).toBeInTheDocument();
    expect(within(roof).getByText("Not ranked")).toBeInTheDocument();
    expect(within(mast).getByRole("cell", { name: "100" })).toBeInTheDocument();
    expect(api.myAnalytics).toHaveBeenCalledTimes(1);
  });

  it("sets the owner's share of a network that counts no node twice", async () => {
    answer();
    renderPage();

    await screen.findByRole("heading", { name: "Your nodes" });
    expect(tile("Your Detections")).toHaveTextContent("150");
    expect(tile("Share of Network")).toHaveTextContent(SHARE);
    expect(tile("Share of Network")).toHaveTextContent(`of ${NETWORK_TOTAL} network detections`);
    expect(tile("Network Detections")).toHaveTextContent(NETWORK_TOTAL);
  });

  it("gives each owned node its leaderboard rank, and says why a private one has none", async () => {
    answer();
    renderPage();

    const table = (await screen.findByRole("heading", { name: "Your nodes" })).closest(".card") as HTMLElement;
    const mast = within(table).getByRole("row", { name: /Mast/ });
    expect(within(mast).getByText("#2")).toBeInTheDocument();
    expect(mast).toHaveTextContent("of 3");
    const roof = within(table).getByRole("row", { name: /Roof/ });
    expect(roof).toHaveTextContent("Its location is private, so it is not published");
    expect(tile("Nodes Ranked")).toHaveTextContent("1 / 2");
    expect(api.leaderboard).toHaveBeenCalled();
  });

  it("puts the network below the owner's section and marks the owner's nodes in it", async () => {
    answer();
    renderPage();

    await screen.findByRole("heading", { name: "Your nodes" });
    const yours = screen.getByRole("heading", { name: /^Your contribution/ });
    const network = screen.getByRole("heading", { name: "Network" });
    expect(yours.compareDocumentPosition(network) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await screen.findByRole("heading", { name: "Network Rankings" });
    const rankings = card("Network Rankings");
    expect(within(within(rankings).getByRole("row", { name: /mast-node|Mast/ })).getByText("Yours")).toBeInTheDocument();
    expect(within(within(rankings).getByRole("row", { name: /Hilltop/ })).queryByText("Yours")).toBeNull();

    const overlaps = card("Coverage Overlaps");
    expect(within(within(overlaps).getByRole("row", { name: /Mast/ })).getByText("Yours")).toBeInTheDocument();
  });

  it("shows an owner with no nodes the claim path and the network, without asking for their figures", async () => {
    answer({ mine: [] });
    renderPage();

    expect(await screen.findByText(CLAIM_PATH)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^Your contribution/ })).toBeInTheDocument();
    expect(await screen.findByText("Network Detections")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your nodes" })).toBeNull();
    expect(api.myAnalytics).not.toHaveBeenCalled();
  });

  it.each([
    ["signed out", () => (state.auth = { user: null, loading: false })],
    [
      "on the admin host",
      () => {
        state.auth = { user: ADA, loading: false };
        state.admin = true;
      },
    ],
  ])("shows the network alone %s, and never asks for owned nodes", async (_, setUp) => {
    setUp();
    answer();
    renderPage();

    expect(await screen.findByText("Network Detections")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Network Rankings" });
    expect(screen.queryByRole("heading", { name: /^Your contribution/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Network" })).toBeNull();
    expect(screen.queryByText("Yours")).toBeNull();
    expect(api.myNodes).not.toHaveBeenCalled();
    expect(api.myAnalytics).not.toHaveBeenCalled();
  });

  it("keeps the network when the owner's figures fail, under a notice of their own", async () => {
    answer();
    vi.mocked(api.myAnalytics).mockRejectedValue(new Error("boom"));
    renderPage();

    await waitFor(() => expect(alertWith("Could not load your nodes' figures")).toBeDefined());
    expect(alertWith("Could not load your nodes' figures")).toHaveClass("notice", "error");
    expect(await screen.findByText("Network Detections")).toBeInTheDocument();
    // Without the owner's own figures the network is the public map as it stands.
    expect(tile("Network Detections")).toHaveTextContent("460");
    expect(screen.queryByRole("heading", { name: "Your nodes" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Network Rankings" })).toBeInTheDocument();
  });

  it("loses only the ranks when the leaderboard fails", async () => {
    answer();
    vi.mocked(api.leaderboard).mockRejectedValue(new Error("down"));
    renderPage();

    const table = (await screen.findByRole("heading", { name: "Your nodes" })).closest(".card") as HTMLElement;
    await waitFor(() => expect(alertWith("Could not load the leaderboard")).toBeDefined());
    expect(alertWith("Could not load the leaderboard")).not.toHaveClass("error");

    // The owner's figures and their share are all there; only the ranks are not.
    expect(within(within(table).getByRole("row", { name: /Roof/ })).getByRole("cell", { name: "50" })).toBeInTheDocument();
    expect(tile("Share of Network")).toHaveTextContent(SHARE);
    expect(within(table).queryByText("Not ranked")).toBeNull();
    expect(within(table).queryByText(/^#\d/)).toBeNull();
    expect(tile("Nodes Ranked")).toHaveTextContent("did not load");
    expect(screen.queryByRole("heading", { name: "Network Rankings" })).toBeNull();
    expect(tile("Network Detections")).toHaveTextContent("470");
  });
});
