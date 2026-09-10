import { useEffect, useRef } from "react";
import { useMap, useMapEvents } from "react-leaflet";
import { buildViewportSnapshot, getFocusPoints } from "./geo";

/**
 * Moves the map on exactly two occasions and no others:
 *
 *   (a) the one-time initial fit, on the first run that has anything to fit;
 *   (b) an explicit `focusNonce` bump — the Toolbar "Fit" button and the
 *       owner-mode switch, both of which are a request about the viewport.
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
 */
export function FitBounds({ aircraft, nodes, selectedHex, focusNonce }) {
  const map = useMap();
  const initialFitted = useRef(false);
  const lastFocusNonce = useRef(focusNonce);

  useEffect(() => {
    const isExplicit = focusNonce !== lastFocusNonce.current;
    if (initialFitted.current && !isExplicit) return;

    const pts = getFocusPoints(aircraft, nodes, selectedHex);

    if (pts.length >= 2) {
      map.fitBounds(pts, { padding: [60, 60], animate: true, duration: 0.5 });
    } else if (pts.length === 1) {
      map.setView(pts[0], map.getZoom(), { animate: true, duration: 0.5 });
    } else {
      // Nothing to fit yet: leave both refs alone so the pending initial fit —
      // or a Fit pressed before the first feed arrived — still happens once
      // there are points.
      return;
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
