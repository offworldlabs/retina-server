import L from "leaflet";

/* ── World-copy wrapping ──────────────────────────────────────────────────
      The basemap repeats east-west forever; Leaflet's projection does not.
      A node at -82° projects into the original copy of the world and nowhere
      else, so panning one world east gives a map that still draws tiles and
      has lost every marker, arc and trail behind it.

      Everything here shifts by the SAME whole number of world widths — the
      copy the viewport is centred on — rather than each element choosing the
      copy nearest itself.  A uniform shift cannot split a shape across the
      seam, and it changes only when the centre crosses ±180°, which is what
      makes a re-projection per crossing affordable instead of one per pan.
      The price is that an element within a screen's width of the antimeridian
      is not drawn while the centre is on the far side of it; nothing the
      fleet has ever reported comes near enough for that to show.

      Latitude has no equivalent and none is wanted: Web Mercator ends at
      ±85° with empty background beyond it, not another copy.  ── */

const WORLD_DEG = 360;

/** Which copy of the world a longitude falls in: 0 for the canonical
 *  [-180, 180), 1 for the copy east of it, -1 for the copy west. */
export function worldCopyIndex(lon) {
  const copies = Math.round(lon / WORLD_DEG);
  return copies === 0 ? 0 : copies; // Math.round hands back -0 west of Greenwich
}

/** `lon` moved into the copy of the world nearest `refLon`. */
export function wrapLonNear(lon, refLon) {
  return lon + WORLD_DEG * worldCopyIndex(refLon - lon);
}

// Held weakly, and off Leaflet's own objects, so a torn-down map neither
// leaks nor collides with an internal field.
const copyByMap = new WeakMap();

/** Sets the copy a map projects into. Returns whether it changed. */
export function setWorldCopy(map, copies) {
  if (getWorldCopy(map) === copies) return false;
  copyByMap.set(map, copies);
  return true;
}

export function getWorldCopy(map) {
  return copyByMap.get(map) ?? 0;
}

/** `latlng` moved into the copy `map` is drawing in. */
function shiftedLatLng(map, latlng) {
  const copies = getWorldCopy(map);
  const ll = L.latLng(latlng);
  return copies === 0 ? ll : L.latLng(ll.lat, ll.lng + copies * WORLD_DEG);
}

/**
 * The inverse of the wrapped projection: a layer point back in the feed's own
 * frame.
 *
 * Leaflet's own `layerPointToLatLng` is left unwrapped, so on a wrapped map it
 * hands back the shifted longitude.  Anything that goes out to layer points to
 * do pixel-space geometry and then stores the result as coordinates has to
 * come back through here, or the shift is banked into the data and applied a
 * second time when the layer draws itself.
 */
export function canonicalLatLng(map, point) {
  const ll = map.layerPointToLatLng(point);
  const copies = getWorldCopy(map);
  return copies === 0 ? ll : L.latLng(ll.lat, ll.lng - copies * WORLD_DEG);
}

/**
 * Puts a map in the copy its centre falls in, re-projecting every layer when
 * that changes.  Returns whether it moved.
 *
 * The re-projection has to be asked for: a pan only translates the map pane,
 * and layer positions are recomputed on a view reset alone.  `viewreset` is
 * the event the layers already listen to for exactly that — markers, vector
 * paths, both canvas renderers, popups and tooltips — and the tile layer
 * treats it as a no-op reposition rather than a reload.
 */
export function syncWorldCopy(map) {
  return applyWorldCopy(map, worldCopyIndex(map.getCenter().lng));
}

/** Puts a map back in the canonical copy, redrawing it there if it had moved. */
export function clearWorldCopy(map) {
  return applyWorldCopy(map, 0);
}

function applyWorldCopy(map, copies) {
  if (!setWorldCopy(map, copies)) return false;
  map.fire("viewreset");
  return true;
}

/**
 * Teaches every Leaflet map to project into its assigned world copy.
 *
 * `latLngToLayerPoint` is the one funnel every drawn element goes through —
 * markers, both canvas renderers, vector paths, popup and tooltip anchors,
 * and this app's own arc geometry — so patching it reaches layers added
 * imperatively at 60 fps as readily as the declarative ones.  Tile layers
 * project by pixel bounds instead and are left alone, which is correct: they
 * already repeat.
 *
 * Leaflet works out how far a setView, panTo or fitBounds has to move through
 * the same call, so a move at constant zoom lands on the instance of its
 * target that is drawn rather than teleporting back to the canonical copy.
 * That is deliberate — Follow has to track where the user is looking — but it
 * does mean `getCenter()` reports a longitude outside [-180, 180] for a
 * wrapped map.  A move that also changes zoom animates to the coordinates it
 * was given and so returns to the canonical copy, which is why Fit sometimes
 * comes home; either way it ends on the fleet with everything drawn.
 *
 * Idempotent, and inert for any map that has no WorldWrap mounted, since an
 * unregistered map sits in copy 0.
 */
export function installWorldWrap() {
  // Cast rather than @ts-nocheck: the patches reach into Leaflet internals the
  // type definitions do not carry, and the rest of the file stays checked.
  const mapProto = L.Map.prototype as any;
  const circleProto = L.Circle.prototype as any;

  // The marker is the idempotency guard, and it survives a dev-server hot
  // reload handing this module a fresh set of module-level state.
  if (mapProto.latLngToLayerPoint.wrapsWorldCopies) return;

  const project = mapProto.latLngToLayerPoint;
  mapProto.latLngToLayerPoint = function (latlng) {
    return project.call(this, shiftedLatLng(this, latlng));
  };
  mapProto.latLngToLayerPoint.wrapsWorldCopies = true;

  // panInside measures through map.project rather than the call above, so on a
  // wrapped map it reads every on-screen marker as off-screen.  Leaflet calls
  // it whenever a marker takes focus (autoPanOnFocus), which a click does — so
  // without this, clicking an aircraft dragged the view a whole world back to
  // the canonical copy.
  const panInside = mapProto.panInside;
  mapProto.panInside = function (latlng, options) {
    return panInside.call(this, shiftedLatLng(this, latlng), options);
  };

  // A metre-radius circle is the one layer that does not go through the call
  // above: it projects through map.project to size its radius against the
  // latitude, and subtracts the pixel origin itself.  Without this the node
  // uncertainty discs and the range rings stay behind in copy 0 while the
  // markers they belong to move.
  const projectCircle = circleProto._project;
  circleProto._project = function () {
    projectCircle.call(this);
    const copies = getWorldCopy(this._map);
    if (copies === 0) return;
    this._point.x += copies * this._map.getPixelWorldBounds().getSize().x;
    this._updateBounds();
  };
}
