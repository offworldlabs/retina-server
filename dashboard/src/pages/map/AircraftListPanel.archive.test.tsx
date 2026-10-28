import { beforeAll, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import AircraftListPanel from "./AircraftListPanel";
import { MapThemeProvider } from "./useMapTheme";

// jsdom has no ResizeObserver, and the list sizes its virtual window with one.
beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

const BASE_FIX = { hex: "a1b2c3", _isTruth: true, lat: 34.85, lon: -82.39, alt_baro: 35000, source: "adsb" };

function renderList(archive?: boolean, fix: Record<string, unknown> = { ...BASE_FIX, gs: 450 }) {
  return render(
    <MapThemeProvider>
      <AircraftListPanel
        allAircraft={[]}
        truthOnly={[fix]}
        selectedHex={null}
        onSelect={() => {}}
        collapsed={false}
        onToggleCollapse={() => {}}
        searchQuery=""
        onSearchChange={() => {}}
        searchInputRef={{ current: null }}
        onSortChange={() => {}}
        pinned={new Set()}
        onTogglePin={() => {}}
        userLoc={null}
        header={null}
        {...(archive === undefined ? {} : { archive })}
      />
    </MapThemeProvider>,
  );
}

const rowSource = (container: HTMLElement) => container.querySelector(".al-row.truth-only")?.textContent ?? "";

describe("the aircraft list's truth-only rows", () => {
  it("read ADS-B in the archive", () => {
    const text = rowSource(renderList(true).container);
    expect(text).toContain("ADS-B");
    expect(text).not.toContain("Truth");
  });

  it.each([undefined, false])("read Truth on the live map, archive %s", (archive) => {
    const text = rowSource(renderList(archive).container);
    expect(text).toContain("Truth");
    expect(text).not.toContain("ADS-B");
  });
});

const speedCell = (container: HTMLElement) => container.querySelector(".al-row .al-spd")?.textContent ?? "";

describe("the aircraft list's speed and heading cells", () => {
  it("show an archived fix without speed or track as unknown", () => {
    const text = speedCell(renderList(true, { ...BASE_FIX, gs: undefined, track: undefined }).container);
    expect(text).toBe("— · —");
  });

  it("show a real 0 kt and 0° for an archived fix", () => {
    const text = speedCell(renderList(true, { ...BASE_FIX, gs: 0, track: 0 }).container);
    expect(text).toBe("0kt · 0°");
  });

  it("show a live fix's speed and track as before", () => {
    const text = speedCell(renderList(false, { ...BASE_FIX, source: "live", gs: 450, track: 90 }).container);
    expect(text).toBe("450kt · 90°");
  });
});
