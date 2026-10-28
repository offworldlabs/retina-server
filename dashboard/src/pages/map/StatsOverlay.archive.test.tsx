import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import StatsOverlay from "./StatsOverlay";
import { MapThemeProvider } from "./useMapTheme";

const panel = (clockMs?: number | null, extra: { truth?: any[]; archive?: boolean } = {}) =>
  render(
    <MapThemeProvider>
      <StatsOverlay
        aircraft={[]}
        truth={extra.truth ?? []}
        anomalyCount={0}
        visible
        onToggle={() => {}}
        clockMs={clockMs}
        archive={extra.archive}
      />
    </MapThemeProvider>,
  );

describe("StatsOverlay header", () => {
  it("says Replay stats when the map has a replay clock", () => {
    panel(Date.parse("2026-10-18T20:14:05Z"));
    expect(screen.getByRole("button", { name: /stats/i })).toHaveTextContent("Replay stats");
  });

  it("says Live stats on the live map", () => {
    panel(null);
    expect(screen.getByRole("button", { name: /stats/i })).toHaveTextContent("Live stats");
  });

  it("says Live stats when no clock is passed", () => {
    panel();
    expect(screen.getByRole("button", { name: /stats/i })).toHaveTextContent("Live stats");
  });

  it("names the region Replay stats when the map has a replay clock", () => {
    panel(Date.parse("2026-10-18T20:14:05Z"));
    expect(screen.getByRole("region", { name: "Replay stats" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Live stats" })).toBeNull();
  });

  it("names the region Live stats on the live map", () => {
    panel(null);
    expect(screen.getByRole("region", { name: "Live stats" })).toBeInTheDocument();
  });
});

describe("StatsOverlay truth count", () => {
  const truth = [{ hex: "a1b2c3" }, { hex: "d4e5f6" }];

  it("counts the archived ADS-B traffic in the archive", () => {
    panel(Date.parse("2026-10-18T20:14:05Z"), { truth, archive: true });
    expect(screen.getByText("+2 ADS-B")).toBeInTheDocument();
    expect(screen.queryByText(/truth/)).toBeNull();
  });

  it("keeps the truth count live", () => {
    panel(null, { truth });
    expect(screen.getByText("+2 truth")).toBeInTheDocument();
    expect(screen.queryByText(/\+2 ADS-B/)).toBeNull();
  });
});
