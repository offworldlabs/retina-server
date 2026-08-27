// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import {
  ADSB_SINGLE_ARC_ICON_MULTIPLE,
  ADSB_SINGLE_COLOR,
  POSITION_SOURCE_ADSB_SINGLE,
} from "./constants";
import { aircraftIconSize } from "./icons";
import { trimAroundAnchor } from "./arcTrim";

/* ── ClaimedArcs: the short locus section under a single-node-claimed aircraft.

      Deliberately NOT part of DetectionArcs.  Those are afterglow: one
      polyline per detection, frozen geometry, fading on its own clock out of
      a ring buffer.  These have a present/absent lifecycle tied to the
      aircraft entry itself — the backend emits the entry only while exactly
      one node holds a fresh claim, so the arc appears with the entry and goes
      when the claim goes.  One polyline per hex, reused across ticks.

      Geometry is a true section of the node's full bistatic locus (shipped on
      the entry as ambiguity_arc), cut to a fixed SCREEN length centred where
      the locus passes the ADS-B fix.  That means it must be re-cut whenever
      the projection or the fix moves — hence the tick and the zoomend hook,
      not a one-shot at creation. ── */
const ClaimedArcs = memo(function ClaimedArcs({ aircraftRef, onSelect }) {
  const map = useMap();
  const polyMapRef = useRef(new Map()); // hex → { line: L.polyline }
  const onSelectRef = useRef(onSelect);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);

  useEffect(() => {
    const polyMap = polyMapRef.current;

    const tick = () => {
      const list = aircraftRef.current || [];
      const seen = new Set();

      for (const ac of list) {
        if (ac.position_source !== POSITION_SOURCE_ADSB_SINGLE) continue;
        const arc = ac.ambiguity_arc;
        if (!Array.isArray(arc) || arc.length < 2) continue;
        if (!Number.isFinite(ac.lat) || !Number.isFinite(ac.lon)) continue;

        // Layer-point space: distances there are screen pixels at the current
        // zoom, which is exactly the unit the trim is specified in.
        const pts = [];
        for (const p of arc) {
          if (!Array.isArray(p) || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) continue;
          pts.push(map.latLngToLayerPoint(L.latLng(p[0], p[1])));
        }
        if (pts.length < 2) continue;

        // Anchor on the DISPLAYED position (dead-reckoned between fixes), not
        // the raw fix, so the arc stays centred under the gliding icon rather
        // than snapping back twice a second.
        const anchor = map.latLngToLayerPoint(L.latLng(ac.lat, ac.lon));
        const lengthPx = ADSB_SINGLE_ARC_ICON_MULTIPLE * aircraftIconSize(ac);
        const cut = trimAroundAnchor(pts, anchor, lengthPx);
        if (!cut || cut.length < 2) continue;

        const latlngs = cut.map((p) => map.layerPointToLatLng(L.point(p.x, p.y)));
        seen.add(ac.hex);

        const existing = polyMap.get(ac.hex);
        if (existing) {
          existing.line.setLatLngs(latlngs);
        } else {
          const hex = ac.hex;
          const line = L.polyline(latlngs, {
            color: ADSB_SINGLE_COLOR,
            weight: 2,
            opacity: 0.9,
            lineCap: "round",
            lineJoin: "round",
          });
          line.on("click", (e) => {
            L.DomEvent.stopPropagation(e);
            onSelectRef.current?.(hex);
          });
          line.addTo(map);
          polyMap.set(hex, { line });
        }
      }

      // Claim went stale (>5 s) or the aircraft left the viewport: the entry
      // is gone from the list, so the arc goes with it — no fade.
      for (const [hex, info] of polyMap) {
        if (seen.has(hex)) continue;
        info.line.remove();
        polyMap.delete(hex);
      }
    };

    // 500 ms matches the 2 fps display-array rebuild upstream; anything faster
    // would re-cut identical geometry.  zoomend re-cuts immediately because a
    // zoom step halves or doubles the arc's pixel length, and waiting out the
    // tick would show it visibly wrong for up to half a second.
    tick();
    const intervalId = setInterval(tick, 500);
    map.on("zoomend", tick);
    return () => {
      clearInterval(intervalId);
      map.off("zoomend", tick);
      for (const info of polyMap.values()) info.line.remove();
      polyMap.clear();
    };
  }, [map, aircraftRef]);

  return null;
});

export default ClaimedArcs;
