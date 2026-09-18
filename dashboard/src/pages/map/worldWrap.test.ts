import { describe, it, expect, beforeEach, afterEach } from "vitest";
import L from "leaflet";
import {
  worldCopyIndex,
  wrapLonNear,
  setWorldCopy,
  getWorldCopy,
  syncWorldCopy,
  installWorldWrap,
  canonicalLatLng,
  clearWorldCopy,
} from "./worldWrap";

describe("worldCopyIndex", () => {
  it("puts the canonical range in copy 0", () => {
    expect(worldCopyIndex(0)).toBe(0);
    expect(worldCopyIndex(-82.39)).toBe(0);
    expect(worldCopyIndex(179.9)).toBe(0);
    expect(worldCopyIndex(-179.9)).toBe(0);
  });

  it("changes copy at each ±180° crossing", () => {
    expect(worldCopyIndex(180.1)).toBe(1);
    expect(worldCopyIndex(359)).toBe(1);
    expect(worldCopyIndex(-180.1)).toBe(-1);
    expect(worldCopyIndex(541)).toBe(2);
  });
});

describe("wrapLonNear", () => {
  it("leaves a longitude already nearest the reference alone", () => {
    expect(wrapLonNear(-82, -90)).toBe(-82);
  });

  it("moves a longitude into the reference's copy", () => {
    expect(wrapLonNear(-82, 278)).toBeCloseTo(278, 6);
    expect(wrapLonNear(-82, -400)).toBeCloseTo(-442, 6);
  });

  it("takes the short way across the antimeridian", () => {
    expect(wrapLonNear(170, -170)).toBeCloseTo(-190, 6);
    expect(wrapLonNear(-170, 170)).toBeCloseTo(190, 6);
  });
});

// jsdom reports a zero-size element, and Leaflet reads the container's size to
// place the viewport centre — so the container is given one.
function sizedContainer(width = 800, height = 600) {
  const el = document.createElement("div");
  Object.defineProperty(el, "clientWidth", { value: width });
  Object.defineProperty(el, "clientHeight", { value: height });
  document.body.appendChild(el);
  return el;
}

describe("the projection patch", () => {
  let container;
  let map;

  beforeEach(() => {
    installWorldWrap();
    container = sizedContainer();
    map = L.map(container, { center: [34.85, -82.39], zoom: 6 });
  });

  afterEach(() => {
    map.remove();
    container.remove();
  });

  it("is inert for a map nobody has moved out of copy 0", () => {
    const before = map.latLngToLayerPoint([34.85, -82.39]);
    expect(getWorldCopy(map)).toBe(0);
    expect(map.latLngToLayerPoint([34.85, -82.39])).toEqual(before);
  });

  it("shifts by exactly one world width per copy, and only in x", () => {
    const home = map.latLngToLayerPoint([34.85, -82.39]);
    // One world width at this zoom, measured the long way round.
    const worldPx = map.latLngToLayerPoint([34.85, 277.61]).x - home.x;
    expect(worldPx).toBeGreaterThan(0);

    setWorldCopy(map, 1);
    const east = map.latLngToLayerPoint([34.85, -82.39]);
    expect(east.x - home.x).toBeCloseTo(worldPx, 6);
    expect(east.y).toBeCloseTo(home.y, 6);

    setWorldCopy(map, -2);
    const west = map.latLngToLayerPoint([34.85, -82.39]);
    expect(west.x - home.x).toBeCloseTo(-2 * worldPx, 6);
    expect(west.y).toBeCloseTo(home.y, 6);
  });

  it("does not stack a second shift when installed again", () => {
    setWorldCopy(map, 1);
    const once = map.latLngToLayerPoint([34.85, -82.39]);
    installWorldWrap();
    expect(map.latLngToLayerPoint([34.85, -82.39])).toEqual(once);
  });

  it("comes back out of layer space in the feed's own frame", () => {
    // The arc layers go out to layer points to trim in pixels and store the
    // result as coordinates; without canonicalLatLng the shift is banked into
    // those coordinates and applied again when the polyline draws itself.
    setWorldCopy(map, 1);
    const point = map.latLngToLayerPoint([34.85, -82.39]);
    // Exactly one world apart; the round trip itself is only pixel-accurate,
    // because Leaflet rounds a layer point to whole pixels.
    expect(map.layerPointToLatLng(point).lng - canonicalLatLng(map, point).lng)
      .toBeCloseTo(360, 6);
    expect(canonicalLatLng(map, point).lng).toBeCloseTo(-82.39, 1);
    expect(canonicalLatLng(map, point).lat).toBeCloseTo(34.85, 1);
  });

  it("round-trips unchanged in copy 0", () => {
    const point = map.latLngToLayerPoint([34.85, -82.39]);
    expect(canonicalLatLng(map, point).lng).toBe(map.layerPointToLatLng(point).lng);
    expect(canonicalLatLng(map, point).lng).toBeCloseTo(-82.39, 1);
  });

  it("takes metre-radius circles along with everything else", () => {
    // A Circle sizes its radius against latitude and so projects its centre
    // its own way; it has to land on the same spot as a CircleMarker at the
    // same coordinates, in any copy.
    // Typed loosely: where a path lands is internal state, with no public
    // read-back that survives projection.
    const circle: any = L.circle([34.85, -82.39], { radius: 5000 }).addTo(map);
    const dot: any = L.circleMarker([34.85, -82.39], { radius: 5 }).addTo(map);
    const gap = () => circle._point.x - dot._point.x;
    const home = gap();
    const radius = circle._radius;

    setWorldCopy(map, 1);
    circle._project();
    dot._project();
    expect(gap()).toBeCloseTo(home, 6);
    // Shifted, not rescaled.
    expect(circle._radius).toBeCloseTo(radius, 6);
    // And its hit/draw box moved with it.
    expect(circle._pxBounds.getCenter().x).toBeCloseTo(circle._point.x, 6);
  });

  it("leaves the view alone when a marker on screen takes focus", () => {
    // Leaflet auto-pans to a focused marker, and a click focuses one.  The
    // marker is under the cursor either way, so nothing should move.
    map.setView([34.85, 277.61], 6, { animate: false });
    syncWorldCopy(map);
    const before = map.getCenter();

    map.panInside([34.85, -82.39]);
    expect(map.getCenter().lat).toBeCloseTo(before.lat, 6);
    expect(map.getCenter().lng).toBeCloseTo(before.lng, 6);
  });

  it("draws a marker in the copy the viewport was panned into", () => {
    const marker = L.marker([34.85, -82.39]).addTo(map);
    const centred = map.latLngToContainerPoint(marker.getLatLng());
    expect(centred.x).toBeCloseTo(400, 0); // the map is centred on it

    // Pan one whole world east, onto the same place in the next copy.
    map.setView([34.85, 277.61], 6);
    const stranded = map.latLngToContainerPoint(marker.getLatLng());
    expect(Math.abs(stranded.x - 400)).toBeGreaterThan(10000); // far off-screen

    expect(syncWorldCopy(map)).toBe(true);
    const followed = map.latLngToContainerPoint(marker.getLatLng());
    expect(followed.x).toBeCloseTo(centred.x, 6);
    expect(followed.y).toBeCloseTo(centred.y, 6);
    // Its coordinates are untouched — only where they are drawn changed.
    expect(marker.getLatLng().lng).toBeCloseTo(-82.39, 6);
  });
});

describe("syncWorldCopy", () => {
  let container;
  let map;

  beforeEach(() => {
    installWorldWrap();
    container = sizedContainer();
    map = L.map(container, { center: [0, 0], zoom: 3 });
  });

  afterEach(() => {
    map.remove();
    container.remove();
  });

  it("does nothing while the centre stays in the same copy", () => {
    expect(syncWorldCopy(map)).toBe(false);
    map.setView([0, 179], 3);
    expect(syncWorldCopy(map)).toBe(false);
    expect(getWorldCopy(map)).toBe(0);
  });

  it("follows the centre across the seam and re-projects once", () => {
    // Counted after the setView, which fires a viewreset of its own.
    map.setView([0, 200], 3);
    let resets = 0;
    map.on("viewreset", () => { resets += 1; });

    expect(syncWorldCopy(map)).toBe(true);
    expect(getWorldCopy(map)).toBe(1);
    expect(resets).toBe(1);

    // Still in copy 1 — no further work.
    map.setView([0, 300], 3);
    resets = 0;
    expect(syncWorldCopy(map)).toBe(false);
    expect(resets).toBe(0);
  });

  it("puts a map back in the canonical copy and redraws it there", () => {
    map.setView([0, 200], 3, { animate: false });
    expect(syncWorldCopy(map)).toBe(true);

    let resets = 0;
    map.on("viewreset", () => { resets += 1; });
    expect(clearWorldCopy(map)).toBe(true);
    expect(getWorldCopy(map)).toBe(0);
    expect(resets).toBe(1);

    expect(clearWorldCopy(map)).toBe(false);
    expect(resets).toBe(1);
  });

  it("comes back west again", () => {
    map.setView([0, -200], 3, { animate: false });
    expect(syncWorldCopy(map)).toBe(true);
    expect(getWorldCopy(map)).toBe(-1);

    map.setView([0, -100], 3, { animate: false });
    expect(syncWorldCopy(map)).toBe(true);
    expect(getWorldCopy(map)).toBe(0);
  });

  it("sends a view move at constant zoom to the copy already on screen", () => {
    map.setView([0, -400], 3, { animate: false });
    expect(syncWorldCopy(map)).toBe(true);

    // Leaflet works out how far to pan through latLngToLayerPoint, so asking
    // to centre on 0° lands on the instance of it that is drawn — one world
    // west — rather than teleporting back to the canonical copy.  That is
    // what keeps Follow tracking where the user is looking.
    map.setView([0, 0], 3, { animate: false });
    expect(map.getCenter().lng).toBeCloseTo(-360, 6);
    expect(syncWorldCopy(map)).toBe(false);
    expect(getWorldCopy(map)).toBe(-1);
  });

  it("comes home on a move that changes zoom", () => {
    map.setView([0, -400], 3, { animate: false });
    expect(syncWorldCopy(map)).toBe(true);

    // A zoom change animates to the coordinates it was given rather than to a
    // pixel offset, so the copy goes with it.  Fit lands here when it rescales.
    map.setView([0, 0], 5, { animate: false });
    expect(map.getCenter().lng).toBeCloseTo(0, 6);
    expect(syncWorldCopy(map)).toBe(true);
    expect(getWorldCopy(map)).toBe(0);
  });
});
