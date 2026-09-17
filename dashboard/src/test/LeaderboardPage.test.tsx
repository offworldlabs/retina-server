import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import LeaderboardPage from "../pages/user/LeaderboardPage";

const state = vi.hoisted(() => ({
  auth: { user: null as { name: string } | null, loading: false },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

/** A published row. The miss-detection fields are absent, which is the shape
 *  /api/admin/leaderboard sends a caller with no session. */
const publicRow = {
  node_ref: "ret-abc",
  name: "ret-abc",
  detections: 7,
  frames: 3,
  tracks: 2,
  uptime_s: 300,
  avg_snr: 11,
  trust_score: 0.5,
  reputation: 0.5,
  online: true,
  rank: 1,
};

const ownerRow = { ...publicRow, in_range: 20, detected_in_range: 12, missed: 8, miss_rate: 0.4 };

function serve(row: object) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ leaderboard: [row], total: 1 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
    )
  );
}

const MISS_COLUMNS = ["In Range", "Missed", "Miss Rate"];

beforeEach(() => {
  state.auth = { user: null, loading: false };
  vi.unstubAllGlobals();
});

describe("the leaderboard shown to a caller with no session", () => {
  beforeEach(() => serve(publicRow));

  it("still ranks the nodes", async () => {
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    // Named twice on the page: once on the podium card, once in the table row.
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
  });

  // The server withholds the fields behind them, so a column of zeroes would
  // report every node as having missed nothing.
  it.each(MISS_COLUMNS)("leaves out the %s column", async (header) => {
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    // Named twice on the page: once on the podium card, once in the table row.
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
    expect(screen.queryAllByText(header)).toHaveLength(0);
  });

  it("does not offer a sort over data it was not sent", async () => {
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    // Named twice on the page: once on the podium card, once in the table row.
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: "Miss Rate" })).not.toBeInTheDocument();
  });
});

describe("the leaderboard shown to a caller with a session", () => {
  beforeEach(() => {
    state.auth = { user: { name: "Ada" }, loading: false };
    serve(ownerRow);
  });

  it.each(MISS_COLUMNS)("keeps the %s column", async (header) => {
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    // Named twice on the page: once on the podium card, once in the table row.
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
    // "Miss Rate" names both the column and its sort button.
    expect(screen.getAllByText(header).length).toBeGreaterThan(0);
  });

  it("reports what the node missed", async () => {
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    // Named twice on the page: once on the podium card, once in the table row.
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
    expect(screen.getByText("40.0%")).toBeInTheDocument();
  });
});
