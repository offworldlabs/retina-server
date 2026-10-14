import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import DashboardLayout from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";
import LeaderboardPage from "../pages/user/LeaderboardPage";
import { resetAuth, setAuth } from "./fakeAuth";
import { stubMatchMedia } from "./matchMedia";

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

const ADA = { id: "u1", email: "ada@example.com", name: "Ada" };
const BOB = { id: "u2", email: "bob@example.com", name: "Bob" };

/** A published row. The miss-detection fields are absent, which is the shape
 *  /api/radar/leaderboard sends a caller with no session. */
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

/** Answers the leaderboard with `row`, and the owner's node list with `mine`,
 *  or with a 500 when `mine` is a number. */
function serve(row: object | object[], mine: object[] | number = []) {
  const rows = Array.isArray(row) ? row : [row];
  const fetch = vi.fn(async (path: string) => {
    if (path.includes("/api/auth/me/nodes") && typeof mine === "number") {
      return new Response("{}", { status: mine, headers: { "Content-Type": "application/json" } });
    }
    const body = path.includes("/api/auth/me/nodes") ? mine : { leaderboard: rows, total: rows.length };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** `count` published rows, ranked by detections in the order given and by
 *  availability in the reverse of it. */
function fleet(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    ...publicRow,
    node_ref: `ret-${i + 1}`,
    name: `Node ${i + 1}`,
    detections: 1000 - i * 10,
    availability_7d: (i + 1) / 100,
    rank: i + 1,
  }));
}

/** One node in the owner's list, as /api/auth/me/nodes names it. */
function owned(ref: string | null, extra: object = {}) {
  return {
    node_id: `owned-${ref}`,
    node_ref: ref,
    name: ref,
    status: "active",
    location_private: false,
    polled: null,
    ...extra,
  };
}

function renderPage() {
  render(
    <MemoryRouter>
      <LeaderboardPage />
    </MemoryRouter>
  );
}

/** The page as the console shows it: inside the layout, which starts it over
 *  whenever the signed-in identity changes. */
function renderInConsole() {
  render(
    <ThemeProvider>
      <MemoryRouter initialEntries={["/leaderboard"]}>
        <DashboardLayout isAdmin={false}>
          <LeaderboardPage />
        </DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>
  );
}

function card(heading: string): HTMLElement {
  return screen.getByRole("heading", { name: heading }).closest(".card") as HTMLElement;
}

/** Each body row of a card's table by its first cell: a rank, a dash, or what
 *  a gap row says. */
function firstCells(within_: HTMLElement): string[] {
  return [...within_.querySelectorAll("tbody tr")].map((r) => r.querySelector("td")!.textContent!);
}

function rowOf(within_: HTMLElement, name: string): HTMLElement {
  return within(within_).getByText(name).closest("tr") as HTMLElement;
}

/** The name in every row marked as the caller's, across the whole page. */
function markedRows(): string[] {
  return [...document.querySelectorAll("tr.mine")].map((r) => r.querySelector(".rank-name .mono")!.textContent!);
}

/** Every path the page asked `fetch` for. */
function paths(fetch: { mock: { calls: unknown[][] } }): string[] {
  return fetch.mock.calls.map(([path]) => String(path));
}

const MISS_COLUMNS = ["In Range", "Missed", "Miss Rate"];

beforeEach(() => {
  resetAuth();
  vi.unstubAllGlobals();
});

describe("the leaderboard shown to a caller with no session", () => {
  beforeEach(() => {
    serve(publicRow);
  });

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
    setAuth({ user: ADA });
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

describe("a signed-in owner's nodes on the leaderboard", () => {
  beforeEach(() => {
    setAuth({ user: ADA });
  });

  it("ranks each of them among the nodes either side", async () => {
    serve(fleet(10), [owned("ret-6")]);
    renderPage();
    await screen.findByRole("heading", { name: "Your nodes" });
    const standing = card("Your nodes");
    expect(firstCells(standing)).toEqual(["4", "5", "6", "7", "8"]);
    const mine = rowOf(standing, "Node 6");
    expect(mine).toHaveClass("mine");
    expect(within(mine).getByText("Yours")).toBeInTheDocument();
    expect(rowOf(standing, "Node 5")).not.toHaveClass("mine");
  });

  it("ranks them by the sort chosen", async () => {
    serve(fleet(10), [owned("ret-6")]);
    renderPage();
    await screen.findByRole("heading", { name: "Your nodes" });
    fireEvent.click(screen.getByRole("button", { name: "Availability" }));
    const standing = card("Your nodes");
    expect(firstCells(standing)).toEqual(["3", "4", "5", "6", "7"]);
    expect(rowOf(standing, "Node 6")).toHaveClass("mine");
    expect(within(rowOf(standing, "Node 6")).getAllByRole("cell")[0]).toHaveTextContent("5");
  });

  it("says how many nodes lie between two of them", async () => {
    serve(fleet(20), [owned("ret-2"), owned("ret-15")]);
    renderPage();
    await screen.findByRole("heading", { name: "Your nodes" });
    expect(firstCells(card("Your nodes"))).toEqual(["1", "2", "3", "4", "8 more", "13", "14", "15", "16", "17"]);
  });

  it("says why one of them is not ranked", async () => {
    serve(fleet(5), [
      owned("ret-2"),
      owned("ret-private", { name: "Garden", location_private: true }),
      owned("ret-polled", { name: "Loft", polled: { trust_state: "probation" } }),
      owned("ret-new", { name: "Shed" }),
    ]);
    renderPage();
    await screen.findByRole("heading", { name: "Your nodes" });
    const standing = card("Your nodes");
    expect(firstCells(standing)).toEqual(["1", "2", "3", "4", "—", "—", "—"]);
    expect(rowOf(standing, "Garden")).toHaveTextContent(/location is private/i);
    expect(rowOf(standing, "Loft")).toHaveTextContent(/probation/i);
    expect(rowOf(standing, "Shed")).toHaveTextContent(/not ranked yet/i);
    for (const name of ["Garden", "Loft", "Shed"]) expect(rowOf(standing, name)).toHaveClass("mine");
  });

  it("marks them in the full rankings and on the podium", async () => {
    serve(fleet(4), [owned("ret-2")]);
    renderPage();
    await screen.findByRole("heading", { name: "Your nodes" });
    const rankings = card("Rankings");
    expect(rowOf(rankings, "Node 2")).toHaveClass("mine");
    expect(within(rowOf(rankings, "Node 2")).getByText("Yours")).toBeInTheDocument();
    expect(rowOf(rankings, "Node 1")).not.toHaveClass("mine");
    expect(within(card("Top Performers")).getAllByText("Yours")).toHaveLength(1);
  });

  it("shows no standing to a caller who owns no nodes", async () => {
    serve(fleet(3), []);
    renderPage();
    await screen.findByRole("heading", { name: "Rankings" });
    expect(screen.queryByRole("heading", { name: "Your nodes" })).not.toBeInTheDocument();
    expect(screen.queryByText("Yours")).not.toBeInTheDocument();
  });

  it("keeps their standing through a failed refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      serve(fleet(10), [owned("ret-6")]);
      renderPage();
      await screen.findByRole("heading", { name: "Your nodes" });
      const failing = serve(fleet(10), 500);
      await vi.advanceTimersByTimeAsync(5 * 60_000);
      await waitFor(() =>
        expect(failing.mock.calls.map(([path]) => path)).toContainEqual(expect.stringContaining("/api/auth/me/nodes"))
      );
      expect(firstCells(card("Your nodes"))).toEqual(["4", "5", "6", "7", "8"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("still ranks the fleet when their nodes fail to load", async () => {
    serve(fleet(3), 500);
    renderPage();
    await waitFor(() => expect(within(card("Rankings")).getByText("Node 3")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Your nodes" })).not.toBeInTheDocument();
  });
});

/* The page holds the owner's list in its own state, fetched once on arrival
   and then every few minutes. What keeps one session's list from showing to
   the next is the layout around it, which starts the page over whenever the
   signed-in identity changes, so these render the two together. */
describe("the owner's nodes across a change of identity", () => {
  beforeEach(() => {
    stubMatchMedia();
  });

  /** Ada signed in, with her standing on screen: she owns Node 6. */
  async function adaOnScreen() {
    setAuth({ user: ADA });
    serve(fleet(10), [owned("ret-6")]);
    renderInConsole();
    await screen.findByRole("heading", { name: "Your nodes" });
    expect(markedRows()).toEqual(["Node 6", "Node 6"]);
  }

  it("finds them as soon as the session is known", async () => {
    setAuth({ loading: true });
    serve(fleet(10), [owned("ret-6")]);
    renderInConsole();
    await screen.findByRole("heading", { name: "Rankings" });
    act(() => setAuth({ user: ADA, loading: false }));
    // Well inside the owner's refresh period, so only the session's arrival
    // can have asked.
    expect(await screen.findByRole("heading", { name: "Your nodes" }, { timeout: 2000 })).toBeInTheDocument();
  });

  it("leaves none of them marked once their owner signs out", async () => {
    await adaOnScreen();
    const fetch = serve(fleet(10), [owned("ret-6")]);
    act(() => setAuth({ user: null }));
    await waitFor(() => expect(paths(fetch)).toContainEqual(expect.stringContaining("/api/radar/leaderboard")));
    await screen.findByRole("heading", { name: "Rankings" });
    expect(screen.queryByRole("heading", { name: "Your nodes" })).not.toBeInTheDocument();
    expect(screen.queryByText("Yours")).not.toBeInTheDocument();
    expect(markedRows()).toEqual([]);
    // Nobody's nodes to ask about once the session has gone.
    expect(paths(fetch)).not.toContainEqual(expect.stringContaining("/api/auth/me/nodes"));
  });

  it("shows another account its own and none of the last one's", async () => {
    await adaOnScreen();
    serve(fleet(10), [owned("ret-2")]);
    act(() => setAuth({ user: BOB }));
    await waitFor(() => expect(rowOf(card("Your nodes"), "Node 2")).toHaveClass("mine"));
    expect(within(card("Your nodes")).queryByText("Node 6")).not.toBeInTheDocument();
    expect(rowOf(card("Rankings"), "Node 6")).not.toHaveClass("mine");
    expect(markedRows()).toEqual(["Node 2", "Node 2"]);
    // Node 2 stands second, so its tag is on the podium too, and on its tile.
    const tags = within(card("Top Performers")).getAllByText("Yours");
    expect(tags).toHaveLength(1);
    expect(tags[0].parentElement).toHaveTextContent("Node 2");
  });

  // The new page has nothing until the new account's list lands, and nothing
  // for good if asking fails: no earlier answer is there to fall back on.
  it.each([
    ["still in flight", () => new Promise<Response>(() => {})],
    ["failed", async () => new Response("{}", { status: 500 })],
  ])("marks none of the last account's while the next one's list is %s", async (_state, answer) => {
    await adaOnScreen();
    const board = { leaderboard: fleet(10), total: 10 };
    const fetch = vi.fn((path: string) =>
      path.includes("/api/auth/me/nodes")
        ? answer()
        : Promise.resolve(new Response(JSON.stringify(board), { status: 200, headers: { "Content-Type": "application/json" } }))
    );
    vi.stubGlobal("fetch", fetch);
    act(() => setAuth({ user: BOB }));
    await waitFor(() => expect(paths(fetch)).toContainEqual(expect.stringContaining("/api/auth/me/nodes")));
    await screen.findByRole("heading", { name: "Rankings" });
    expect(screen.queryByRole("heading", { name: "Your nodes" })).not.toBeInTheDocument();
    expect(screen.queryByText("Yours")).not.toBeInTheDocument();
    expect(markedRows()).toEqual([]);
  });

  // The provider sets the identity afresh whenever /api/auth/me answers. The
  // same account is not a change, so the standing stays and is not asked for.
  it("keeps the standing through a fresh copy of the same identity", async () => {
    await adaOnScreen();
    const fetch = serve(fleet(10), [owned("ret-6")]);
    act(() => setAuth({ user: { ...ADA } }));
    expect(screen.getByRole("heading", { name: "Your nodes" })).toBeInTheDocument();
    expect(markedRows()).toEqual(["Node 6", "Node 6"]);
    expect(paths(fetch)).toEqual([]);
  });
});

describe("a caller with no session", () => {
  // Nobody's nodes to find, and asking would be a 401 every refresh.
  it("is not asked about nodes of their own", async () => {
    const fetch = serve(fleet(3), [owned("ret-2")]);
    renderPage();
    await screen.findByRole("heading", { name: "Rankings" });
    expect(fetch.mock.calls.map(([path]) => path)).not.toContainEqual(expect.stringContaining("/api/auth/me/nodes"));
    expect(screen.queryByRole("heading", { name: "Your nodes" })).not.toBeInTheDocument();
  });
});

describe("the podium", () => {
  it("gives each of the top three a drawn medal, named for its place", async () => {
    serve(fleet(5));
    renderPage();
    await screen.findByRole("heading", { name: "Top Performers" });
    const podium = card("Top Performers");
    const medals = within(podium).getAllByRole("img");
    expect(medals.map((m) => m.getAttribute("aria-label"))).toEqual(["1st place", "2nd place", "3rd place"]);
    for (const [place, metal, node] of [
      ["1st place", "gold", "Node 1"],
      ["2nd place", "silver", "Node 2"],
      ["3rd place", "bronze", "Node 3"],
    ]) {
      const medal = within(podium).getByRole("img", { name: place });
      expect(medal.tagName.toLowerCase()).toBe("svg");
      expect(medal).toHaveClass("podium-medal", metal);
      // On the tile of the node in that place.
      expect(medal.parentElement).toHaveTextContent(node);
    }
  });

  // An emoji is drawn by whatever emoji font the platform has, or not at all.
  it("draws no emoji", async () => {
    serve(fleet(5));
    renderPage();
    await screen.findByRole("heading", { name: "Top Performers" });
    expect(card("Top Performers").textContent).not.toMatch(/\p{Extended_Pictographic}/u);
  });

  it("awards only the places there are", async () => {
    serve(fleet(2));
    renderPage();
    await screen.findByRole("heading", { name: "Top Performers" });
    const medals = within(card("Top Performers")).getAllByRole("img");
    expect(medals.map((m) => m.getAttribute("aria-label"))).toEqual(["1st place", "2nd place"]);
  });
});

describe("searching the rankings", () => {
  it("numbers a row by its rank, not by its place among the matches", async () => {
    serve(fleet(10));
    renderPage();
    await screen.findByRole("heading", { name: "Rankings" });
    fireEvent.change(screen.getByPlaceholderText("Search…"), { target: { value: "Node 7" } });
    expect(firstCells(card("Rankings"))).toEqual(["7"]);
  });
});
