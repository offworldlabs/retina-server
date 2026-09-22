import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import rules from "../pages/map/LiveAircraftMap.css?raw";
import NodeOwnerControl from "../pages/map/NodeOwnerControl";
import StatsOverlay from "../pages/map/StatsOverlay";
import { MapThemeProvider } from "../pages/map/useMapTheme";

/* The owner control and the live stats sit in the aircraft list's header as
   ui.css cards; the map's stylesheet gives them only spacing and type. */
describe("the aircraft list's header panels", () => {
  it("are shared cards", () => {
    const owner = render(
      <NodeOwnerControl user={{ name: "Owner" }} ownedCount={1} ownerOnly={false} onToggle={() => {}} loading={false} />,
    );
    expect(owner.container.querySelector(".owner-panel")).toHaveClass("card");

    const stats = render(
      <MapThemeProvider>
        <StatsOverlay aircraft={[]} truth={[]} anomalyCount={0} visible onToggle={() => {}} />
      </MapThemeProvider>,
    );
    expect(stats.container.querySelector(".stats-panel")).toHaveClass("card");
  });

  it.each([".stats-panel {", ".owner-panel {"])("%s restates none of the card", (anchor) => {
    const block = rules.slice(rules.indexOf(anchor));
    const body = block.slice(0, block.indexOf("}"));
    expect(body).not.toMatch(/background|border|border-radius/);
  });
});
