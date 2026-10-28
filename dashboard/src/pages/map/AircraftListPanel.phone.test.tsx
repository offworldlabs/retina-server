import { beforeAll, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import AircraftListPanel from "./AircraftListPanel";
import { MapThemeProvider } from "./useMapTheme";

// jsdom has no ResizeObserver, and the list sizes its virtual window with one.
beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

function renderList(collapsed: boolean) {
  return render(
    <MapThemeProvider>
      <AircraftListPanel
        allAircraft={[{ hex: "abc123", lat: 51, lon: -1 }, { hex: "def456", lat: 52, lon: -1 }]}
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
        header={null}
      />
    </MapThemeProvider>,
  );
}

// Collapsed on a phone the list is a chip over the map, and the title is
// what says what the chip opens (LiveAircraftMap.css, layout.phone.test.ts).
describe("the aircraft list's title", () => {
  it.each([false, true])("names the list and counts it, collapsed %s", (collapsed) => {
    const { container } = renderList(collapsed);
    const title = container.querySelector(".al-title");
    expect(title).toHaveTextContent(/^Aircraft\s*2$/);
    expect(title?.querySelector(".al-count")).toHaveTextContent("2");
  });
});
