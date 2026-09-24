import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  availability_7d: 0.9731,
  avg_snr: 11,
  trust_score: 0.5,
  reputation: 0.5,
  online: true,
  rank: 1,
};

const ownerRow = { ...publicRow, in_range: 20, detected_in_range: 12, missed: 8, miss_rate: 0.4 };

function serve(row: object | object[]) {
  const rows = Array.isArray(row) ? row : [row];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ leaderboard: rows, total: rows.length }), {
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

describe("availability on the leaderboard", () => {
  function tableRow(name: string) {
    // Named twice on the page: the table row is the second.
    return screen.getAllByText(name)[1].closest("tr")!;
  }

  it("reads as the share of the week", async () => {
    serve(publicRow);
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
    expect(within(tableRow("ret-abc")).getByText("97.3%")).toBeInTheDocument();
  });

  it("reads as a dash for a node not yet measured", async () => {
    serve({ ...publicRow, availability_7d: null });
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getAllByText("ret-abc").length).toBeGreaterThan(0));
    expect(within(tableRow("ret-abc")).getByText("—")).toBeInTheDocument();
  });

  it("sorts the most available first, with a node not yet measured last", async () => {
    serve([
      { ...publicRow, node_ref: "ret-a", name: "Low", detections: 30, availability_7d: 0.5 },
      { ...publicRow, node_ref: "ret-b", name: "Unmeasured", detections: 20, availability_7d: null },
      { ...publicRow, node_ref: "ret-c", name: "High", detections: 10, availability_7d: 0.99 },
    ]);
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole("button", { name: "Availability" }));
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1].textContent)).toEqual(["High", "Low", "Unmeasured"]);
  });
});

describe("a node's name on the leaderboard", () => {
  it("is shown whole rather than cut like a ref", async () => {
    serve({ ...publicRow, name: "Ada's rooftop receiver" });
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    // Named twice on the page: once on the podium card, once in the table row.
    await waitFor(() => expect(screen.getAllByText("Ada's rooftop receiver")).toHaveLength(2));
  });

  // The route answers with the whole ref as the name when a node carries none
  // of its own, which would otherwise read as a very long name.
  it("falls back to the short ref when the node has no name of its own", async () => {
    serve({ ...publicRow, node_ref: "nde4f2k9xq7m3b8", name: "nde4f2k9xq7m3b8" });
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getAllByText("4f2k9xq7m3b8")).toHaveLength(2));
    expect(screen.queryByText("nde4f2k9xq7m3b8")).not.toBeInTheDocument();
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
