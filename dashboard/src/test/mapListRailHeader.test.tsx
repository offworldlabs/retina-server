import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import AircraftListPanel from "../pages/map/AircraftListPanel";

// jsdom has no ResizeObserver, and the rail sizes its virtual list with one.
beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

function renderRail(collapsed: boolean) {
  return render(
    <AircraftListPanel
      allAircraft={[]}
      truthOnly={[]}
      selectedHex={null}
      onSelect={() => {}}
      collapsed={collapsed}
      onToggleCollapse={() => {}}
      searchQuery=""
      onSearchChange={() => {}}
      searchInputRef={{ current: null }}
      onSortChange={() => {}}
      pinned={new Set()}
      onTogglePin={() => {}}
      userLoc={null}
      header={<div data-testid="rail-header">controls</div>}
    />,
  );
}

describe("the aircraft list rail's header", () => {
  it("carries the controls it is given", () => {
    renderRail(false);
    expect(screen.getByTestId("rail-header")).toBeInTheDocument();
  });

  it("hides them with the rest of the rail when collapsed", () => {
    renderRail(true);
    expect(screen.queryByTestId("rail-header")).toBeNull();
  });
});
