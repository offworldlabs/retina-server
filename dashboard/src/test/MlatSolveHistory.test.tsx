import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SolveHistoryTable } from "../pages/admin/MlatSolveHistory";
import { DASH } from "../utils/format";

type History = Parameters<typeof SolveHistoryTable>[0]["history"];
type Solve = NonNullable<History["solves"]>[number];

const PRODUCTION = { host: "admin.retina.fm", protocol: "https:" };

const solve = (over: Partial<Solve> = {}): Solve => ({
  ts_ms: Date.UTC(2026, 9, 18, 22, 14, 7, 1),
  n_nodes: 3,
  gt_error_km: null,
  heading_err_deg: null,
  rms_delay: 0.4,
  rms_doppler: 1.2,
  gt_hex: null,
  outcome: "published",
  solver_hex: "mn0123456789",
  lat: 34.8521,
  lon: -82.394,
  raw_lat: 34.85,
  raw_lon: -82.39,
  archive_map: "shown",
  ...over,
});

function table(solves: Solve[], at = PRODUCTION) {
  render(<SolveHistoryTable history={{ hex: "mn0123456789", window_minutes: 30, solves }} at={at} />);
}

const mapHref = () => screen.getByRole("link", { name: "Show on map" }).getAttribute("href");

describe("the solve history's map column", () => {
  it("opens a shown solve on the app's archive map, selected, the second after it was published", () => {
    table([solve()]);
    const link = screen.getByRole("link", { name: "Show on map" });
    expect(link).toHaveAttribute(
      "href",
      "https://app.retina.fm/map#lat=34.8521&lon=-82.3940&z=10&hex=mn0123456789&t=2026-10-18T22:14:08Z",
    );
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("keeps a publish time that is already on the second", () => {
    table([solve({ ts_ms: Date.UTC(2026, 9, 18, 22, 14, 7) })]);
    expect(mapHref()).toMatch(/&t=2026-10-18T22:14:07Z$/);
  });

  it("links to the app beside a local admin console", () => {
    table([solve()], { host: "admin.localhost:5191", protocol: "http:" });
    expect(mapHref()).toMatch(/^http:\/\/app\.localhost:5191\/map#/);
  });

  it("opens a known-lane publish, which has only a raw position, at that position", () => {
    table([solve({ lat: null, lon: null, raw_lat: 34.8521, raw_lon: -82.394 })]);
    expect(mapHref()).toMatch(/^https:\/\/app\.retina\.fm\/map#lat=34\.8521&lon=-82\.3940&/);
  });

  it("disables the link, saying why, for a solve the archive map leaves out", () => {
    table([
      solve({ archive_map: "unpublished" }),
      solve({ archive_map: "synthetic" }),
      solve({ archive_map: "private" }),
      solve({ lat: null, lon: null, raw_lat: null, raw_lon: null }),
    ]);
    const reasons = [
      "Not published, so not in the archive.",
      "Synthetic fleet: the archive map shows the real fleet only.",
      "A private node contributed: the public archive leaves this solve out.",
      "No position recorded for this solve.",
    ];
    const buttons = screen.getAllByRole("button", { name: "Show on map" });
    expect(buttons).toHaveLength(reasons.length);
    buttons.forEach((button, i) => {
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("title", reasons[i]);
      expect(button).toHaveAccessibleDescription(reasons[i]);
    });
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("shows a dash when the backend does not say", () => {
    table([solve({ archive_map: undefined })]);
    const row = screen.getAllByRole("row")[1];
    expect(within(row).queryByRole("link")).toBeNull();
    expect(within(row).queryByRole("button")).toBeNull();
    const cells = within(row).getAllByRole("cell");
    expect(cells[cells.length - 1]).toHaveTextContent(DASH);
  });
});
