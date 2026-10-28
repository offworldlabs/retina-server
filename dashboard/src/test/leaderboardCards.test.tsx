import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import LeaderboardPage from "../pages/user/LeaderboardPage";
import { resetAuth, setAuth } from "./fakeAuth";
import { stubViewport } from "./matchMedia";

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

const row = { frames: 3, tracks: 2, availability_7d: 0.5, avg_snr: 11, online: true, in_range: 20, missed: 8, miss_rate: 0.4 };

/** Alpha leads on detections, Gamma on trust. */
const ROWS = [
  { ...row, node_ref: "ret-a", name: "Alpha", detections: 300, trust_score: 0.1 },
  { ...row, node_ref: "ret-b", name: "Beta", detections: 200, trust_score: 0.5 },
  { ...row, node_ref: "ret-c", name: "Gamma", detections: 100, trust_score: 0.9 },
];

const MINE = [{ node_id: "owned-b", node_ref: "ret-b", name: "Beta", status: "active", location_private: false, polled: null }];

function serve() {
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    const body = path.includes("/api/auth/me/nodes") ? MINE : { leaderboard: ROWS, total: ROWS.length };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

async function renderPage() {
  render(
    <MemoryRouter>
      <LeaderboardPage />
    </MemoryRouter>
  );
  await waitFor(() => expect(screen.getAllByText("Gamma").length).toBeGreaterThan(0));
}

function rankings(): HTMLElement {
  return screen.getByRole("heading", { name: "Rankings" }).closest(".card") as HTMLElement;
}

function rankingRows(): HTMLElement[] {
  return [...rankings().querySelectorAll<HTMLElement>("tbody tr")];
}

function firstCells(): string[] {
  return rankingRows().map((r) => r.querySelector("td")!.textContent! + " " + r.querySelector(".mono")!.textContent!);
}

function cellOf(rowEl: HTMLElement, label: string): HTMLElement {
  return rowEl.querySelector(`td[data-label="${label}"]`) as HTMLElement;
}

beforeEach(() => {
  resetAuth();
  vi.unstubAllGlobals();
  serve();
  act(() => setAuth({ user: { id: "u1", email: "ada@example.com", name: "Ada" } }));
});

describe("the leaderboard on a phone", () => {
  it("swaps the sort buttons for a Sort by select on each listing", async () => {
    stubViewport(390);
    await renderPage();
    expect(screen.queryByRole("button", { name: "Trust" })).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Sort by")).toHaveLength(2);
  });

  it("re-sorts the rankings from either select and keeps both in step", async () => {
    stubViewport(390);
    await renderPage();
    expect(firstCells()).toEqual(["1 Alpha", "2 Beta", "3 Gamma"]);
    fireEvent.change(screen.getAllByLabelText("Sort by")[0], { target: { value: "trust" } });
    expect(firstCells()).toEqual(["1 Gamma", "2 Beta", "3 Alpha"]);
    for (const select of screen.getAllByLabelText("Sort by")) expect((select as HTMLSelectElement).value).toBe("trust");
  });

  it("heads each card with the rank and node, and keeps the sorted reading in view", async () => {
    stubViewport(390);
    await renderPage();
    const first = rankingRows()[0];
    expect(first.querySelector("td")!.className).toBe("leaderboard-rank");
    expect(first.querySelector("td")!.getAttribute("data-label")).toBe("");
    expect(first.querySelectorAll("td")[1].getAttribute("data-hint")).toBe("title");
    expect(cellOf(first, "Detections").hasAttribute("data-hint")).toBe(false);
    expect(cellOf(first, "Trust").getAttribute("data-hint")).toBe("secondary");
    fireEvent.change(screen.getAllByLabelText("Sort by")[1], { target: { value: "trust" } });
    const top = rankingRows()[0];
    expect(cellOf(top, "Trust").hasAttribute("data-hint")).toBe(false);
    expect(cellOf(top, "Detections").getAttribute("data-hint")).toBe("secondary");
  });
});

describe("the leaderboard on a desktop", () => {
  it("keeps the sort buttons and has no select", async () => {
    stubViewport(1280);
    await renderPage();
    expect(screen.getByRole("button", { name: "Trust" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Sort by")).not.toBeInTheDocument();
  });
});

describe("the leaderboard's gap rows", () => {
  it("carries the note it shows in data-gap", async () => {
    stubViewport(1280);
    const fleet = Array.from({ length: 20 }, (_, i) => ({
      ...row,
      node_ref: `ret-${i + 1}`,
      name: `Node ${i + 1}`,
      detections: 1000 - i * 10,
      trust_score: 0.5,
    }));
    const mine = ["ret-2", "ret-15"].map((ref) => ({ ...MINE[0], node_id: `owned-${ref}`, node_ref: ref, name: ref }));
    vi.stubGlobal("fetch", vi.fn(async (path: string) => {
      const body = path.includes("/api/auth/me/nodes") ? mine : { leaderboard: fleet, total: fleet.length };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>
    );
    const standing = (await screen.findByRole("heading", { name: "Your nodes" })).closest(".card") as HTMLElement;
    const gap = standing.querySelector<HTMLElement>("tr.rank-gap > td")!;
    expect(gap.textContent).toBe("8 more");
    expect(gap.getAttribute("data-gap")).toBe("8 more");
  });
});
