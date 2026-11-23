import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import ContributionPage from "../pages/user/ContributionPage";
import type { Leaderboard, OwnerAnalytics, OwnerSummary } from "../types";
import { stubViewport } from "./matchMedia";
import { ownedNode } from "./fixtures/ownedNode";

vi.mock("../api/client", () => ({
  api: { analytics: vi.fn(), overlaps: vi.fn(), leaderboard: vi.fn(), myNodes: vi.fn(), myAnalytics: vi.fn() },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { id: 1, email: "a@example.com", name: "Ada" }, loading: false }),
}));

const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
const HILL = "ndehill00000001";

const summary = (ref: string, detections: number): OwnerSummary => ({
  node_id: `id-${ref}`,
  node_ref: ref,
  trust: { trust_score: 0.9 },
  detection_area: {},
  metrics: { total_detections: detections, availability_7d: 0.95 },
  reputation: {},
  coverage_map: null,
  empirical_coverage: null,
  node_bias: null,
});

const OWN: OwnerAnalytics = { nodes: { [MAST.node_ref]: summary(MAST.node_ref, 100) } };
const BOARD: Leaderboard = {
  leaderboard: [
    {
      node_ref: HILL,
      name: "Hilltop",
      rank: 1,
      detections: 300,
      frames: 0,
      tracks: 0,
      availability_7d: 0.9,
      avg_snr: 10,
      trust_score: 0.9,
      reputation: 0,
      online: true,
      rank_status: null,
      system_constant_db: null,
      system_constant_sigma_db: null,
      range_50pct_km: null,
      range_50pct_bound: null,
    },
  ],
  total: 1,
};

/** The owner's node listed with no rank, ahead of Hilltop on detections. */
const UNRANKED: Leaderboard = {
  leaderboard: [
    ...BOARD.leaderboard,
    { ...BOARD.leaderboard[0], node_ref: MAST.node_ref, name: "Mast", rank: null, rank_status: "low_evidence", detections: 900 },
  ],
  total: 2,
};

async function open(board: Leaderboard = BOARD) {
  vi.mocked(api.analytics).mockResolvedValue({ nodes: { [HILL]: summary(HILL, 300) } });
  vi.mocked(api.overlaps).mockResolvedValue({ overlaps: [], registered_nodes: [] });
  vi.mocked(api.leaderboard).mockResolvedValue(board);
  vi.mocked(api.myNodes).mockResolvedValue([MAST]);
  vi.mocked(api.myAnalytics).mockResolvedValue(OWN);
  render(
    <MemoryRouter>
      <ContributionPage />
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: "Network Rankings" });
  await screen.findByText("Detections per Node — Top 20");
}

const rows = (title: string) =>
  [...(screen.getByRole("heading", { name: title }).closest(".card") as HTMLElement).querySelectorAll("tbody > tr")];

describe("the Contribution page's tables on a phone", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("heads a node's card with its name and keeps its availability behind More", async () => {
    stubViewport(390);
    await open();

    const node = rows("Your nodes")[0].querySelectorAll("td");
    expect(node[0].getAttribute("data-hint")).toBe("title");
    expect(node[4].getAttribute("data-hint")).toBe("secondary");
    expect(node[4].getAttribute("data-label")).toBe("Availability (7 d)");
  });

  it("heads a ranking's card with the node, beside a rank that has no label", async () => {
    stubViewport(390);
    await open();

    const ranking = rows("Network Rankings")[0].querySelectorAll("td");
    expect(ranking[0].getAttribute("data-label")).toBe("");
    expect(ranking[1].getAttribute("data-hint")).toBe("title");
  });

  it("gives a node with no rank the reason in place of a number", async () => {
    stubViewport(390);
    await open(UNRANKED);

    const rank = rows("Your nodes")[0].querySelectorAll("td")[3];
    expect(rank).toHaveTextContent("Not ranked");
    expect(rank).toHaveTextContent("Not enough solid evidence to rank it fairly yet: few samples, no clear detection curve, or no tags or claims to check its setup");
    expect(rank).not.toHaveTextContent("#");
    expect(screen.getByText("none ranked yet")).toBeInTheDocument();
  });

  it("leaves a node with no rank out of Network Rankings and of the count", async () => {
    stubViewport(390);
    await open(UNRANKED);

    const listed = rows("Network Rankings");
    expect(listed).toHaveLength(1);
    expect(listed[0]).toHaveTextContent("Hilltop");
    expect(screen.getByText("Top 1 of 1")).toBeInTheDocument();
  });

  it("adds no More to a row at desktop width", async () => {
    stubViewport(1280);
    await open();

    for (const title of ["Your nodes", "Network Rankings"]) {
      for (const tr of rows(title)) expect(tr.querySelector("[data-card-more]")).toBeNull();
    }
  });
});
