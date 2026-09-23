import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import MlatVerificationPage from "../pages/admin/MlatVerificationPage";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: { mlatVerification: vi.fn(), mlatAccuracy: vi.fn(), mlatHistory: vi.fn() },
}));

const track = (solver_hex: string, truth_hex: string, position_error_km: number) => ({
  solver_hex,
  truth_hex,
  position_error_km,
  velocity_error_ms: 4.2,
  altitude_error_m: 120,
  n_nodes: 3,
  rms_delay: 0.41,
  rms_doppler: 1.8,
  object_type: "aircraft",
  is_anomalous: false,
  max_bistatic_angle_deg: 42.5,
  timestamp_ms: Date.now() - 5_000,
});

const VERIFICATION = {
  n_solves: 9,
  n_matched: 3,
  n_unique_aircraft: 5,
  match_rate_pct: 60,
  match_threshold_km: 10,
  tracks: [
    track("mn00000000a1", "a1a1a1", 1.2),
    track("mn00000000b2", "b2b2b2", 9.5),
    track("mn00000000c3", "c3c3c3", 4.0),
  ],
};

const HISTORY = {
  hex: "mn00000000b2",
  window_minutes: 30,
  solves: [
    { ts_ms: Date.now() - 2_000, n_nodes: 4, gt_error_km: 0.42, heading_err_deg: 12, rms_delay: 0.3, rms_doppler: 1.1, gt_hex: "b2b2b2" },
    { ts_ms: Date.now() - 9_000, n_nodes: 3, gt_error_km: 9.1, heading_err_deg: null, rms_delay: 0.7, rms_doppler: 2.4, gt_hex: "d4d4d4" },
  ],
  rejects_nearby: { n: 3, by_outcome: { rejected_gdop: 2, n2_disagree: 1 } },
};

function visit(at = "/mlat") {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <MlatVerificationPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.mlatVerification).mockReset().mockResolvedValue(VERIFICATION);
  vi.mocked(api.mlatAccuracy).mockReset().mockResolvedValue({ n_samples: 0 });
  vi.mocked(api.mlatHistory).mockReset().mockResolvedValue(HISTORY);
});

describe("the admin MLAT page", () => {
  it("lists every matched aircraft, worst position error first, each linking to its history", async () => {
    visit();
    const links = await screen.findAllByRole("link", { name: /^mn/ });
    expect(links.map((a) => a.textContent)).toEqual(["mn00000000b2", "mn00000000c3", "mn00000000a1"]);
    expect(links[0]).toHaveAttribute("href", "/mlat?hex=mn00000000b2");
    const worst = links[0].closest("tr")!;
    expect(within(worst).getByText("b2b2b2")).toBeInTheDocument();
    expect(within(worst).getByText("9.50 km")).toBeInTheDocument();
  });

  it("gives the match rate against the distinct aircraft behind the solves", async () => {
    visit();
    expect(await screen.findByText("3 of 5 aircraft")).toBeInTheDocument();
  });

  it("asks for no solve history until an aircraft is chosen", async () => {
    visit();
    await screen.findAllByRole("link", { name: /^mn/ });
    // The render that lists them runs its effects a tick later.
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(api.mlatHistory).not.toHaveBeenCalled();
  });

  it("opens the chosen aircraft's solve history, with the rejections near it", async () => {
    visit("/mlat?hex=mn00000000b2");
    expect(await screen.findByText("Solve history for mn00000000b2 (matched to b2b2b2 in the latest snapshot)")).toBeInTheDocument();
    expect(await screen.findByText("0.42 km")).toBeInTheDocument();
    expect(api.mlatHistory).toHaveBeenCalledWith("mn00000000b2");
    expect(screen.getByText("12°")).toBeInTheDocument();
    // A re-bind of the truth match shows as a different hex down the column.
    expect(screen.getByText("d4d4d4")).toBeInTheDocument();
    expect(screen.getByText("Rejected nearby: gdop 2, disagree 1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "mn00000000b2" })).toHaveAttribute("aria-current", "true");
  });

  // The backend lower-cases the hex it echoes, so an address typed or pasted
  // in upper case has to be read the same way.
  it("opens the history for a hex written in upper case", async () => {
    visit("/mlat?hex=MN00000000B2");
    expect(await screen.findByText("0.42 km")).toBeInTheDocument();
    expect(api.mlatHistory).toHaveBeenCalledWith("mn00000000b2");
  });

  // The history sits above the list, so a link arriving with an aircraft
  // chosen is taken to it rather than left at the top of the stats.
  it("brings a chosen aircraft's history into view", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      visit("/mlat?hex=mn00000000b2");
      await screen.findByText("0.42 km");
      expect(scroll).toHaveBeenCalled();
      expect((scroll.mock.contexts[0] as Element).textContent).toContain("Solve history for mn00000000b2");
    } finally {
      delete (Element.prototype as Partial<Element>).scrollIntoView;
    }
  });

  // What loaded for the previous aircraft is not on screen for this one, so the
  // notice must not offer it as a stale answer.
  it("says a newly chosen aircraft's history could not load, not that an old one is shown", async () => {
    visit("/mlat?hex=mn00000000b2");
    await screen.findByText("0.42 km");
    vi.mocked(api.mlatHistory).mockRejectedValue(new Error("boom"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    fireEvent.click(screen.getByRole("link", { name: "mn00000000c3" }));
    expect(await screen.findByText(/Could not load the solve history: boom/)).toBeInTheDocument();
    expect(screen.queryByText(/Showing what was loaded/)).toBeNull();
    expect(screen.queryByText("0.42 km")).toBeNull();
  });

  it("follows a click on an aircraft to its history", async () => {
    visit();
    fireEvent.click(await screen.findByRole("link", { name: "mn00000000b2" }));
    expect(await screen.findByText(/^Solve history for mn00000000b2/)).toBeInTheDocument();
    expect(await screen.findByText("0.42 km")).toBeInTheDocument();
    expect(api.mlatHistory).toHaveBeenCalledWith("mn00000000b2");
  });
});
