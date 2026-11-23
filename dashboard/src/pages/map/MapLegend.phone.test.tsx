import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import MapLegend from "./MapLegend";
import { MapThemeProvider } from "./useMapTheme";
import { stubViewport } from "../../test/matchMedia";

// On a phone the legend is a chip that opens its key for a glance; the wide
// screen's open or shut legend is a stored preference the chip never touches.

const STORED = "retina.map.legendOpen";

function mount() {
  const { container } = render(
    <MapThemeProvider>
      <MapLegend colorByAlt={false} showGroundTruth={false} showIlluminators={false} hasArchive />
    </MapThemeProvider>,
  );
  return container.querySelector(".map-legend") as HTMLElement;
}

const header = () => screen.getByRole("button", { name: "Track source" });
const body = () => document.querySelector(".map-legend-body");

beforeEach(() => localStorage.setItem(STORED, "true"));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("the legend on a phone", () => {
  it("is a chip, shut even where the wide screen left the legend open, and up with the archive bar", () => {
    stubViewport(390);
    const legend = mount();
    expect(legend).toHaveClass("map-legend-chip", "with-archive");
    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(body()).toBeNull();
  });

  it("opens on a tap and shuts on a second tap or Escape, storing none of it", () => {
    stubViewport(390);
    const onMapKey = vi.fn();
    window.addEventListener("keydown", onMapKey);
    mount();
    fireEvent.click(header());
    expect(header()).toHaveAttribute("aria-expanded", "true");
    expect(body()).toHaveTextContent("Node");
    fireEvent.click(header());
    expect(body()).toBeNull();
    expect(localStorage.getItem(STORED)).toBe("true");

    fireEvent.click(header());
    fireEvent.keyDown(header(), { key: "Escape" });
    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(body()).toBeNull();
    // Kept from the map, whose Escape would deselect the aircraft in view.
    expect(onMapKey).not.toHaveBeenCalled();
    expect(localStorage.getItem(STORED)).toBe("true");
    window.removeEventListener("keydown", onMapKey);
  });

  it("is shut again when the window narrows to a phone's width", () => {
    const viewport = stubViewport(1440);
    mount();
    expect(body()).not.toBeNull();
    viewport.resize(390);
    expect(header()).toHaveAttribute("aria-expanded", "false");
    expect(body()).toBeNull();
    expect(localStorage.getItem(STORED)).toBe("true");
  });
});

describe("the legend at desktop width", () => {
  it("opens and shuts as the stored choice, as before", () => {
    stubViewport(1440);
    const legend = mount();
    expect(legend).not.toHaveClass("map-legend-chip");
    expect(header()).toHaveAttribute("aria-expanded", "true");
    expect(body()).not.toBeNull();
    fireEvent.click(header());
    expect(body()).toBeNull();
    expect(localStorage.getItem(STORED)).toBe("false");
  });
});
