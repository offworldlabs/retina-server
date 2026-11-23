import { act, cleanup, render } from "@testing-library/react";
import { useEffect } from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

// A browser that animates, as every one the map ships to does: Leaflet reads
// both when it loads, and jsdom has neither. The fit's animation then runs on
// the fake clock.
vi.hoisted(() => {
  window.requestAnimationFrame = (cb: FrameRequestCallback) => setTimeout(() => cb(Date.now()), 16) as unknown as number;
  window.cancelAnimationFrame = (id: number) => clearTimeout(id);
  (window as unknown as { WebKitCSSMatrix: unknown }).WebKitCSSMatrix = class { m11 = 1; };
});

import type { Map as LeafletMap } from "leaflet";
import { MapContainer, useMap } from "react-leaflet";
import { viewHeldFromHash } from "./hashHold";
import { FitBounds } from "./MapControls";

// FitBounds in a real react-leaflet MapContainer: when the map moves on its
// own, and when a deep link's view is left where the link put it.

// jsdom lays nothing out; Leaflet reads its container's size from these.
beforeAll(() => {
  for (const [prop, size] of [["clientWidth", 800], ["clientHeight", 600]] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get(this: HTMLElement) { return this.classList.contains("leaflet-container") ? size : 0; },
    });
  }
});
afterAll(() => {
  delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
  delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight;
});

let map: LeafletMap;
let fitBounds: ReturnType<typeof vi.spyOn>;
let setView: ReturnType<typeof vi.spyOn>;

/** Takes the map, and watches its moves, ahead of FitBounds: a sibling's
 *  effects run in order, so these are in place for FitBounds' first run. */
function Probe() {
  const m = useMap();
  useEffect(() => {
    map = m;
    fitBounds = vi.spyOn(m, "fitBounds");
    setView = vi.spyOn(m, "setView");
  }, [m]);
  return null;
}

interface Props {
  aircraft?: object[];
  nodes?: object[];
  selectedHex?: string | null;
  focusNonce?: number;
  skipInitialFit?: boolean;
}

function Harness({ aircraft = [], nodes = [], selectedHex = null, focusNonce = 0, skipInitialFit }: Props) {
  return (
    <MapContainer center={[40, -100]} zoom={7}>
      <Probe />
      <FitBounds aircraft={aircraft} nodes={nodes} selectedHex={selectedHex} focusNonce={focusNonce} skipInitialFit={skipInitialFit} />
    </MapContainer>
  );
}

const AIRCRAFT = [{ hex: "a1", lat: 52.2, lon: -0.3 }, { hex: "a2", lat: 51.8, lon: -0.9 }];
const MORE = [...AIRCRAFT, { hex: "a3", lat: 52.6, lon: 0.4 }];
/** A signed-in owner's own nodes, drawn where they really are. */
const OWN = [
  { node_ref: "nde-roof", rx_lat: 52.1357, rx_lon: -0.4321, location_private: false },
  { node_ref: "nde-shed", rx_lat: 53.1234, rx_lon: -1.5678, location_private: true },
];
/** The view a deep link opened the map on. */
const LINKED = { lat: 40, lng: -100, zoom: 7 };

let view: ReturnType<typeof render>;
function show(props: Props = {}) {
  view = render(<Harness {...props} />);
  settle();
}
function update(props: Props) {
  view.rerender(<Harness {...props} />);
  settle();
}
/** Long enough for a fit's half-second animation to land. */
function settle() {
  act(() => { vi.advanceTimersByTime(1000); });
}
const moves = () => fitBounds.mock.calls.length;
const where = () => ({ lat: map.getCenter().lat, lng: map.getCenter().lng, zoom: map.getZoom() });

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("FitBounds", () => {
  it("fits once on the first data, and leaves later data alone", () => {
    show();
    expect(moves()).toBe(0);
    update({ aircraft: AIRCRAFT });
    expect(moves()).toBe(1);
    expect(where()).not.toEqual(LINKED);
    update({ aircraft: MORE });
    expect(moves()).toBe(1);
  });

  it.each([[false, 1], [true, 0]])("with skipInitialFit=%s, fits %i time(s) on data it mounts with", (skip, fits) => {
    show({ aircraft: AIRCRAFT, skipInitialFit: skip });
    expect(moves()).toBe(fits);
  });

  it("with skipInitialFit, leaves the linked view alone on the first data and after", () => {
    show({ skipInitialFit: true });
    update({ aircraft: AIRCRAFT, skipInitialFit: true });
    update({ aircraft: MORE, skipInitialFit: true });
    update({ aircraft: MORE, nodes: OWN, selectedHex: "a1", skipInitialFit: true });
    expect(moves()).toBe(0);
    expect(setView).not.toHaveBeenCalled();
    expect(where()).toEqual(LINKED);
  });

  it("with skipInitialFit, still fits on an explicit focusNonce bump", () => {
    show({ aircraft: AIRCRAFT, skipInitialFit: true, focusNonce: 0 });
    expect(moves()).toBe(0);
    update({ aircraft: AIRCRAFT, skipInitialFit: true, focusNonce: 1 });
    expect(moves()).toBe(1);
    expect(where()).not.toEqual(LINKED);
    update({ aircraft: MORE, skipInitialFit: true, focusNonce: 1 });
    expect(moves()).toBe(1);
    update({ aircraft: MORE, skipInitialFit: true, focusNonce: 2 });
    expect(moves()).toBe(2);
  });

  it("with skipInitialFit, a bump before any data fits once the data comes", () => {
    show({ skipInitialFit: true, focusNonce: 0 });
    update({ skipInitialFit: true, focusNonce: 1 });
    expect(moves()).toBe(0);
    update({ aircraft: AIRCRAFT, skipInitialFit: true, focusNonce: 1 });
    expect(moves()).toBe(1);
    update({ aircraft: MORE, skipInitialFit: true, focusNonce: 1 });
    expect(moves()).toBe(1);
  });

  it("reads skipInitialFit at mount only", () => {
    show({ skipInitialFit: false });
    update({ aircraft: AIRCRAFT, skipInitialFit: true });
    expect(moves()).toBe(1);
    cleanup();

    show({ skipInitialFit: true });
    update({ aircraft: AIRCRAFT, skipInitialFit: false });
    update({ aircraft: MORE, skipInitialFit: false });
    expect(moves()).toBe(0);
  });

  it("holds nothing from the hash when it skips a fit on an owner's true positions", () => {
    show({ skipInitialFit: true });
    update({ nodes: OWN, skipInitialFit: true });
    expect(moves()).toBe(0);
    expect(viewHeldFromHash(map)).toBe(false);
    // Fit, pressed: the view now frames the true positions, so it is held.
    update({ nodes: OWN, skipInitialFit: true, focusNonce: 1 });
    expect(moves()).toBe(1);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("without skipInitialFit, holds the first fit on an owner's true positions as before", () => {
    show();
    update({ nodes: OWN });
    expect(moves()).toBe(1);
    expect(viewHeldFromHash(map)).toBe(true);
  });
});
