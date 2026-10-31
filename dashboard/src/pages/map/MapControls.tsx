import { useEffect, useRef } from "react";
import { useMap, useMapEvents } from "react-leaflet";
import { buildViewportSnapshot, getFocus } from "./geo";
import { holdViewForFit, releaseViewOnNavigation } from "./hashHold";
import { isOwnerEntry } from "./nodeListing";
import { clearWorldCopy, installWorldWrap, syncWorldCopy } from "./worldWrap";

/**
 * Moves the map on exactly two occasions and no others:
 *
 *   (a) the one-time initial fit, on the first run that has anything to fit;
 *   (b) an explicit `focusNonce` bump — the Toolbar "Fit" button and the
 *       owner-mode switch, both of which are a request about the viewport.
 *
 * `skipInitialFit` drops (a), for a map opened on a deep link whose hash names
 * the view: the first data would otherwise move the camera off the view the
 * link was sent for. It is read once, at mount, like the hash itself; a later
 * change of the prop does nothing. (b) still fits.
 *
 * Everything else that re-runs this effect — a feed update, and above all a
 * selection change — leaves the camera alone.  Selecting an aircraft asks to
 * inspect it, not to relocate the viewport the user set, and that has to hold
 * whether or not they have panned yet: a viewport nobody has touched is still
 * theirs.  So the gate is the fit history, not user activity, and the former
 * userMoved / deselection guards are gone with it — both existed only to
 * suppress the selection-driven refits this no longer performs.
 *
 * `selectedHex` stays an input so an explicit Fit centres on the selection
 * rather than the whole fleet; it cannot move the map on its own.
 *
 * A fit that frames a signed-in owner's own nodes lands on their true
 * position, so it holds the view back from the URL hash until the user moves
 * the map off it themselves (hashHold.ts). A skipped initial fit holds
 * nothing: the view stays the link's own, which the hash already carries.
 */
export function FitBounds({ aircraft, nodes, selectedHex, focusNonce, skipInitialFit = false }) {
  const map = useMap();
  // Counted as done from the start when skipped; useRef keeps its first value.
  const initialFitted = useRef(Boolean(skipInitialFit));
  const lastFocusNonce = useRef(focusNonce);

  useEffect(() => releaseViewOnNavigation(map), [map]);

  useEffect(() => {
    const isExplicit = focusNonce !== lastFocusNonce.current;
    if (initialFitted.current && !isExplicit) return;

    const { points: pts, nodes: framed } = getFocus(aircraft, nodes, selectedHex);
    if (pts.length === 0) {
      // Nothing to fit yet: leave both refs alone so the pending initial fit —
      // or a Fit pressed before the first feed arrived — still happens once
      // there are points.
      return;
    }

    holdViewForFit(map, framed.some(isOwnerEntry));
    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [60, 60], animate: true, duration: 0.5 });
    } else {
      map.setView(pts[0], map.getZoom(), { animate: true, duration: 0.5 });
    }
    initialFitted.current = true;
    lastFocusNonce.current = focusNonce;
  }, [aircraft, nodes, selectedHex, focusNonce, map]);

  return null;
}

export function MapClickClear({ onClear }) {
  useMapEvents({
    click: () => onClear(),
  });
  return null;
}

export function ViewportTracker({ onChange }) {
  const map = useMapEvents({
    moveend: () => onChange(buildViewportSnapshot(map.getBounds())),
    zoomend: () => onChange(buildViewportSnapshot(map.getBounds())),
    resize: () => onChange(buildViewportSnapshot(map.getBounds())),
  });

  useEffect(() => {
    onChange(buildViewportSnapshot(map.getBounds()));
  }, [map, onChange]);

  return null;
}

/**
 * Keeps the drawn elements in the copy of the world the viewport is centred
 * on, so they follow the repeating basemap east and west instead of staying
 * behind in the original copy (see map/worldWrap).
 */
export function WorldWrap() {
  const map = useMap();
  useEffect(() => {
    installWorldWrap();
    const sync = () => syncWorldCopy(map);
    sync();
    // `move` rather than `moveend` so a drag across the seam re-projects under
    // the cursor; every frame but the crossing itself returns early.
    map.on("move", sync);
    return () => {
      map.off("move", sync);
      // Redraws in the canonical copy on the way out, so nothing is left
      // stranded should this ever unmount while the map lives on.
      clearWorldCopy(map);
    };
  }, [map]);
  return null;
}

/**
 * Keeps Leaflet's cached container size honest.
 *
 * Leaflet measures its container once and then only on a WINDOW resize.  The
 * things that actually resize this map do not touch the window: the aircraft
 * list collapsing to 36px, the detail panel opening, the toolbar rewrapping to
 * a different height.  After any of those the tile layer went on covering the
 * old rectangle and the container background showed through as a blank band
 * down one side — invisible while that background was navy under darkened
 * tiles, obvious the moment it became light grey under Positron.
 *
 * A ResizeObserver catches every case rather than the one prop we thought of,
 * and the callbacks are coalesced into a frame because a CSS width transition
 * fires it continuously for its whole duration.
 */
export function InvalidateSizeOnResize() {
  const map = useMap();
  useEffect(() => {
    if (typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => map.invalidateSize({ animate: false }));
    });
    ro.observe(map.getContainer());
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
    };
  }, [map]);
  return null;
}
