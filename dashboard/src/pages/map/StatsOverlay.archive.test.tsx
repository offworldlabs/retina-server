import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import StatsOverlay from "./StatsOverlay";
import { MapThemeProvider } from "./useMapTheme";

const panel = (clockMs?: number | null) =>
  render(
    <MapThemeProvider>
      <StatsOverlay aircraft={[]} truth={[]} anomalyCount={0} visible onToggle={() => {}} clockMs={clockMs} />
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
