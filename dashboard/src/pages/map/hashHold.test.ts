import { fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A browser that animates zoom, as every one the map ships to does: Leaflet
// reads both when it loads, and jsdom has neither. With them an animated zoom
// starts its move in the next frame, as it does in a browser, rather than at
// once, and the frames run on the fake clock.
vi.hoisted(() => {
  window.requestAnimationFrame = (cb: FrameRequestCallback) => setTimeout(() => cb(Date.now()), 16) as unknown as number;
  window.cancelAnimationFrame = (id: number) => clearTimeout(id);
  (window as unknown as { WebKitCSSMatrix: unknown }).WebKitCSSMatrix = class { m11 = 1; };
});

import L from "leaflet";
import { holdViewForFit, releaseView, releaseViewOnNavigation, viewHeldFromHash } from "./hashHold";

// A real Leaflet map in jsdom, with every navigation handler at its default,
// as LiveAircraftMap's MapContainer leaves them.

let map: L.Map;
let uninstall: () => void;
let size = { w: 800, h: 600 };
/** Whether the view was held at each moveend, in order: what HashSync reads. */
let heldAtMoveend: boolean[];

function newMap(options: L.MapOptions = {}): L.Map {
  const el = document.createElement("div");
  // jsdom lays nothing out; Leaflet reads the size from these two.
  Object.defineProperty(el, "clientWidth", { configurable: true, get: () => size.w });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => size.h });
  document.body.appendChild(el);
  return L.map(el, { center: [52, -0.5], zoom: 9, ...options });
}

/** A map whose view a fit has just held, as FitBounds leaves it. */
function heldMap(options: L.MapOptions = {}) {
  map = newMap(options);
  uninstall = releaseViewOnNavigation(map);
  holdViewForFit(map, true);
  heldAtMoveend = [];
  map.on("moveend", () => heldAtMoveend.push(viewHeldFromHash(map)));
}

beforeEach(() => {
  vi.useFakeTimers();
  size = { w: 800, h: 600 };
  // Map.Keyboard restores the page's scroll after focusing the map.
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  uninstall();
  map.remove();
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const container = () => map.getContainer();
const settle = (ms = 1000) => vi.advanceTimersByTime(ms);
const view = () => ({ centre: map.getCenter(), zoom: map.getZoom() });
const sameView = (a: ReturnType<typeof view>, b: ReturnType<typeof view>) => a.centre.equals(b.centre) && a.zoom === b.zoom;

/** A left-button mouse event. jsdom leaves the legacy `which` at 0, and
 *  Leaflet's drag and box zoom start only on which or button 1. */
function mouse(type: string, target: EventTarget, init: MouseEventInit) {
  const e = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, ...init });
  Object.defineProperty(e, "which", { value: 1 });
  target.dispatchEvent(e);
}

/** A press, a move of (dx, dy) and a release, as Map.Drag (or with shift held,
 *  Map.BoxZoom) sees them: both listen for the move and the release on the
 *  document, which they reach from the element under the pointer. */
function drag(dx: number, dy: number, init: MouseEventInit = {}) {
  mouse("mousedown", container(), { clientX: 400, clientY: 300, ...init });
  mouse("mousemove", container(), { clientX: 400 + dx, clientY: 300 + dy, ...init });
  mouse("mouseup", container(), { clientX: 400 + dx, clientY: 300 + dy, ...init });
}

/** A touch event with `points` fingers down, `spread` px apart from `x`. */
function touch(type: string, target: EventTarget, points: number, spread = 50, x = 300) {
  const e = new Event(type, { bubbles: true, cancelable: true });
  const touches = Array.from({ length: points }, (_, i) => ({ clientX: x + spread * i, clientY: 300, identifier: i }));
  Object.defineProperty(e, "touches", { value: touches });
  target.dispatchEvent(e);
}

/** Two fingers down, spread to `spread` px, then lifted, as Map.TouchZoom
 *  follows them: the start on the map, the rest on the document. */
function pinch(spread: number) {
  touch("touchstart", container(), 2, 50);
  if (spread !== 50) touch("touchmove", document, 2, spread);
  touch("touchend", document, 0);
}

const zoomButton = (which: "in" | "out") => container().querySelector(`.leaflet-control-zoom-${which}`)!;

/** A key pressed on the map. Map.Keyboard listens only while the map has
 *  focus, which a click gives it. */
function key(init: KeyboardEventInit & { keyCode: number }) {
  container().focus();
  fireEvent.keyDown(container(), init);
}
const ARROW_LEFT = { key: "ArrowLeft", keyCode: 37 };
const PLUS = { key: "+", keyCode: 187 };
const MINUS = { key: "-", keyCode: 189 };

/** The map's centre, and a point well away from it, on its 800x600. */
const CENTRE = { clientX: 400, clientY: 300 };
const AWAY = { clientX: 600, clientY: 200 };

describe("holdViewForFit", () => {
  beforeEach(() => heldMap());

  it("holds a view framed on a true position, and lets a later fit on anything else go", () => {
    expect(viewHeldFromHash(map)).toBe(true);
    holdViewForFit(map, false);
    expect(viewHeldFromHash(map)).toBe(false);
    holdViewForFit(map, true);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("holds one map's view and not another's", () => {
    const other = newMap();
    expect(viewHeldFromHash(other)).toBe(false);
    holdViewForFit(other, true);
    holdViewForFit(map, false);
    expect(viewHeldFromHash(other)).toBe(true);
    expect(viewHeldFromHash(map)).toBe(false);
    other.remove();
  });

  it("is let go for a view Follow centres on an aircraft", () => {
    releaseView(map);
    expect(viewHeldFromHash(map)).toBe(false);
  });

  it("holds the fit's own moveend, animated or not", () => {
    // Within a screen: Leaflet animates the pan, and its moveend comes at the end.
    map.setView([52.3, -0.1], 9, { animate: true, duration: 0.5 });
    // Further than a screen: it jumps, and the moveend comes at once.
    holdViewForFit(map, true);
    map.setView([40, -100], 9);
    settle();
    expect(heldAtMoveend).toEqual([true, true]);
  });
});

describe("the user's own move ends the hold before its moveend", () => {
  beforeEach(() => heldMap());

  it("starts a zoom in the frame after the double-click, as a browser does", () => {
    // What the window has to outlast: the double-click's own turn ends with no move.
    let started = 0;
    map.on("movestart", () => { started += 1; });
    fireEvent.dblClick(container(), AWAY);
    expect(started).toBe(0);
    settle(16);
    expect(started).toBe(1);
    settle();
    expect(viewHeldFromHash(map)).toBe(false);
  });

  it.each<[string, () => void]>([
    ["drags the map", () => drag(120, 40)],
    ["box-zooms with shift held", () => drag(120, 40, { shiftKey: true })],
    ["double-clicks the map away from its centre", () => fireEvent.dblClick(container(), AWAY)],
    ["turns the wheel away from the map's centre", () => fireEvent.wheel(container(), { deltaY: -120, ...AWAY })],
    ["pans with an arrow key", () => key(ARROW_LEFT)],
    ["pinches off the map's centre", () => pinch(150)],
  ])("when the user %s", (_, gesture) => {
    const before = view();
    gesture();
    // The wheel waits out its debounce, an animated zoom or pan its frames.
    settle();
    expect(sameView(view(), before)).toBe(false);
    expect(heldAtMoveend.length).toBeGreaterThan(0);
    expect(heldAtMoveend[heldAtMoveend.length - 1]).toBe(false);
    expect(viewHeldFromHash(map)).toBe(false);
  });

  it("when the user drags while the fit is still panning, the cut-off fit's moveend kept back", () => {
    map.setView([52.3, -0.1], 9, { animate: true, duration: 0.5 });
    settle(100);
    drag(120, 40);
    settle();
    // Leaflet stops the fit, which ends it with a moveend part-way, then
    // starts the drag: the first is held, the drag's own is not.
    expect(heldAtMoveend).toEqual([true, false]);
  });

  it("when the user pans with an arrow key after a zoom about the centre", () => {
    key(PLUS);
    settle();
    expect(viewHeldFromHash(map)).toBe(true);
    key(ARROW_LEFT);
    settle();
    expect(heldAtMoveend[heldAtMoveend.length - 1]).toBe(false);
    expect(viewHeldFromHash(map)).toBe(false);
  });
});

describe("a zoom about the centre keeps the hold, as the centre is still the fit's", () => {
  beforeEach(() => heldMap());

  it.each<[string, () => void]>([
    ["the + key", () => key(PLUS)],
    ["the - key", () => key(MINUS)],
    ["the + key with shift, three steps at once", () => key({ ...PLUS, shiftKey: true })],
    ["the zoom-in button", () => fireEvent.click(zoomButton("in"))],
    ["the zoom-out button", () => fireEvent.click(zoomButton("out"))],
    ["a double-click at the centre", () => fireEvent.dblClick(container(), CENTRE)],
    ["the wheel at the centre", () => fireEvent.wheel(container(), { deltaY: -120, ...CENTRE })],
    ["a box drawn about the centre", () => {
      mouse("mousedown", container(), { clientX: 340, clientY: 260, shiftKey: true });
      mouse("mousemove", container(), { clientX: 460, clientY: 340, shiftKey: true });
      mouse("mouseup", container(), { clientX: 460, clientY: 340, shiftKey: true });
    }],
    ["a pinch about the centre", () => {
      touch("touchstart", container(), 2, 50, 375);
      touch("touchmove", document, 2, 150, 325);
      touch("touchend", document, 0);
    }],
  ])("%s", (_, gesture) => {
    const before = view();
    gesture();
    settle();
    expect(view().zoom).not.toBe(before.zoom);
    expect(heldAtMoveend.length).toBeGreaterThan(0);
    expect(heldAtMoveend.every(Boolean)).toBe(true);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("zooming in and out, with the keys and the buttons in turn", () => {
    key(PLUS);
    settle();
    fireEvent.click(zoomButton("in"));
    settle();
    key(MINUS);
    settle();
    fireEvent.click(zoomButton("out"));
    settle();
    expect(map.getZoom()).toBe(9);
    expect(heldAtMoveend).toEqual([true, true, true, true]);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  // Leaflet zooms a key or a button about the centre the pan has drawn, and
  // the wheel, which stops the pan first, about the centre it stopped at.
  it.each<[string, () => void]>([
    ["the + key", () => key(PLUS)],
    ["the zoom-in button", () => fireEvent.click(zoomButton("in"))],
    ["the wheel at the centre", () => fireEvent.wheel(container(), { deltaY: -120, ...CENTRE })],
  ])("%s while the fit is still panning, about where the fit has got to", (_, gesture) => {
    map.setView([52.3, -0.1], 9, { animate: true, duration: 0.5 });
    settle(100);
    gesture();
    settle();
    expect(map.getZoom()).toBe(10);
    expect(heldAtMoveend.length).toBeGreaterThan(0);
    expect(heldAtMoveend.every(Boolean)).toBe(true);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("and a pan the user did not make after it is still held", () => {
    key(PLUS);
    settle();
    // As a popup's auto-pan makes one, once the zoom is over.
    map.panBy([60, 0], { animate: false });
    expect(heldAtMoveend).toEqual([true, true]);
    expect(viewHeldFromHash(map)).toBe(true);
  });
});

describe("a gesture that moves nothing keeps the hold", () => {
  // Leaflet answers a zoom key at the zoom limit with a moveend at the view it
  // already had, and the fit lands its own moveend whatever key was pressed
  // while it panned: written, either would carry the fit's centre.
  it.each<[string, L.MapOptions, () => void]>([
    ["the + key at the zoom limit", { maxZoom: 9 }, () => key(PLUS)],
    ["the - key at the zoom limit", { minZoom: 9 }, () => key(MINUS)],
    ["the zoom-in button at the zoom limit", { maxZoom: 9 }, () => fireEvent.click(zoomButton("in"))],
    ["the wheel at the zoom limit", { maxZoom: 9 }, () => fireEvent.wheel(container(), { deltaY: -120, clientX: 400, clientY: 300 })],
    ["two fingers that never move", {}, () => pinch(50)],
    ["a box given up with Escape", {}, () => {
      mouse("mousedown", container(), { clientX: 400, clientY: 300, shiftKey: true });
      mouse("mousemove", container(), { clientX: 520, clientY: 340, shiftKey: true });
      fireEvent.keyDown(document, { key: "Escape", keyCode: 27 });
      mouse("mouseup", container(), { clientX: 520, clientY: 340, shiftKey: true });
    }],
  ])("%s", (_, options, gesture) => {
    heldMap(options);
    const before = view();
    gesture();
    settle();
    expect(sameView(view(), before)).toBe(true);
    expect(heldAtMoveend.every(Boolean)).toBe(true);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("an arrow key while the fit is still panning, which Leaflet ignores", () => {
    heldMap();
    map.setView([52.3, -0.1], 9, { animate: true, duration: 0.5 });
    settle(100);
    key(ARROW_LEFT);
    settle();
    expect(map.getCenter().lat).toBeCloseTo(52.3, 2);
    expect(heldAtMoveend).toEqual([true]);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("and its window closes: a pan the user did not make after it is still held", () => {
    heldMap({ maxZoom: 9 });
    key(PLUS);
    settle(100);
    // As a popup's auto-pan makes one.
    map.panBy([60, 0], { animate: false });
    expect(heldAtMoveend).toEqual([true, true]);
    expect(viewHeldFromHash(map)).toBe(true);
  });
});

describe("the hold survives moves the user does not make", () => {
  beforeEach(() => heldMap());

  it.each<[string, () => void]>([
    ["a click on the map", () => {
      mouse("mousedown", container(), { clientX: 400, clientY: 300 });
      mouse("mouseup", container(), { clientX: 400, clientY: 300 });
      mouse("click", container(), { clientX: 400, clientY: 300 });
    }],
    ["a press that moves less than a drag", () => drag(1, 1)],
    ["Escape", () => key({ key: "Escape", keyCode: 27 })],
    ["a letter shortcut", () => key({ key: "f", keyCode: 70 })],
    ["Tab or Enter on the map", () => { key({ key: "Tab", keyCode: 9 }); key({ key: "Enter", keyCode: 13 }); }],
    ["an arrow key held with Ctrl", () => key({ ...ARROW_LEFT, ctrlKey: true })],
    ["an arrow key held with Cmd", () => key({ ...ARROW_LEFT, metaKey: true })],
    ["an arrow key held with Alt", () => key({ ...ARROW_LEFT, altKey: true })],
    ["an arrow key pressed outside the map", () => {
      const input = document.body.appendChild(document.createElement("input"));
      input.focus();
      fireEvent.keyDown(input, ARROW_LEFT);
    }],
    ["a one-finger touch", () => {
      touch("touchstart", container(), 1);
      touch("touchend", document, 0);
    }],
    ["a programmatic pan, as Follow and a popup's auto-pan make", () => {
      map.panBy([200, 0], { animate: false });
      map.panTo([52.5, -0.2], { animate: false });
    }],
    ["a wheel over a popup, which the popup keeps from the map", () => {
      const popup = L.popup().setLatLng([52, -0.5]).setContent("<p>node</p>").openOn(map);
      fireEvent.wheel(popup.getElement()!.querySelector("p")!, { deltaY: -120 });
    }],
  ])("%s", (_, act) => {
    act();
    settle();
    expect(heldAtMoveend.every(Boolean)).toBe(true);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("Tab onto a marker at the edge, which Leaflet pans into view", () => {
    const edge = map.containerPointToLatLng([797, 300]);
    const marker = L.marker(edge, { icon: L.divIcon({ iconSize: [20, 20] }) }).addTo(map);
    key({ key: "Tab", keyCode: 9 });
    // Where the browser takes focus next, in the same turn.
    (marker.getElement() as HTMLElement).focus();
    settle();
    expect(heldAtMoveend.length).toBeGreaterThan(0);
    expect(heldAtMoveend.every(Boolean)).toBe(true);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("a resize, which ends in a moveend at the same centre", () => {
    const centre = map.getCenter();
    size = { w: 1200, h: 600 };
    // What InvalidateSizeOnResize calls when a panel opens or the list folds.
    map.invalidateSize({ animate: false });
    expect(heldAtMoveend).toEqual([true]);
    // The same centre to the hash's four decimals, give or take the pixel.
    expect(map.getCenter().lat).toBeCloseTo(centre.lat, 3);
    expect(map.getCenter().lng).toBeCloseTo(centre.lng, 3);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("a gesture whose handler is off", () => {
    map.dragging.disable();
    map.boxZoom.disable();
    map.scrollWheelZoom.disable();
    map.doubleClickZoom.disable();
    map.keyboard.disable();
    map.touchZoom.disable();
    drag(120, 40);
    drag(120, 40, { shiftKey: true });
    fireEvent.wheel(container(), { deltaY: -120 });
    fireEvent.dblClick(container());
    key(ARROW_LEFT);
    pinch(150);
    settle();
    expect(heldAtMoveend).toEqual([]);
    expect(viewHeldFromHash(map)).toBe(true);
  });

  it("the fit's own move, with a gesture's window still open", () => {
    // A wheel turned just before a fit: the fit closes the window, so its own
    // movestart does not count as the user's.
    fireEvent.wheel(container(), { deltaY: -120, clientX: 400, clientY: 300 });
    holdViewForFit(map, true);
    map.setView([40, -100], 9);
    settle();
    expect(heldAtMoveend[0]).toBe(true);
  });
});

it("stops listening once uninstalled", () => {
  heldMap();
  uninstall();
  drag(120, 40);
  drag(120, 40, { shiftKey: true });
  fireEvent.dblClick(container());
  fireEvent.wheel(container(), { deltaY: -120 });
  key(ARROW_LEFT);
  fireEvent.click(zoomButton("in"));
  pinch(150);
  settle();
  expect(heldAtMoveend.length).toBeGreaterThan(0);
  expect(heldAtMoveend.every(Boolean)).toBe(true);
  expect(viewHeldFromHash(map)).toBe(true);
  uninstall = () => {};
});
