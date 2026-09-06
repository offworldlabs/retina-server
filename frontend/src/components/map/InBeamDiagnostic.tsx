// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { pointInPolygon, haversineDistanceKm } from "./geo";
import { groundTruthKey } from "./constants";

/* ── InBeamDiagnostic: flags ADS-B aircraft inside a node's MEASURED
      coverage that have no recent confirmed detection from that node.
      Renders a dashed red polyline from the node's RX position to the
      aircraft — one per (aircraft, node) pair — so the "missing link" is
      geometrically visible.  Reads detectionsRef as the recently-detected
      oracle — a "hex|node_id" → ts map covering every detection shape
      (single-node and multinode), TTL-pruned so its grace period matches
      the spec (don't flag a detection that only just expired).

      Membership used to be a declared-beam test (azimuth, width, range,
      tightened by fudge factors to stop edge-clipping aircraft flagging).
      Those numbers are configuration, so the layer was reporting gaps
      against a wedge the node had never been shown to cover: a mis-declared
      azimuth painted the map red, and a node whose real lobe sat outside its
      declared one had its true gaps hidden.  The test is now the node's own
      empirical polygon, which is what "this node can see here" means, and a
      node with no measured coverage yet is skipped entirely — there is
      nothing to be missing from. ── */

const InBeamDiagnostic = memo(function InBeamDiagnostic({ detectionsRef, groundTruthRef, nodesByIdRef, smoothRef }) {
  const map = useMap();
  const polyMapRef = useRef(new Map()); // pairKey → L.polyline

  useEffect(() => {
    const polyMap = polyMapRef.current;

    const tick = () => {
      // "(hex, node_id) recently detected" map, maintained in the aircraft
      // feed. A present key means that node contributed a detection for that
      // aircraft within the grace window (ARC_TOTAL_LIFE_MS).
      const recentDetections = detectionsRef.current || {};

      const truth = groundTruthRef.current || {};
      const nodes = nodesByIdRef?.current || {};
      const seen = new Set();

      const smooth = smoothRef?.current || {};

      // Pre-resolve the node list once per tick, so the O(truth × nodes) loop
      // below rejects distant pairs with two comparisons instead of a
      // point-in-polygon walk — enabling this layer on a dense testmap used
      // to hang the tab.  The bounding box comes from the polygon itself (its
      // furthest vertex), so the prefilter can never exclude a point the
      // polygon test would have accepted.
      //
      // rx_lat/rx_lon are the server-published coordinates, displaced from the
      // operator's true position by the backend; no true receiver position
      // reaches the browser. The polygon is translated rigidly onto that same
      // anchor, so a ray drawn from it agrees with the served coverage by
      // construction.
      const nodeList = [];
      for (const [nodeId, node] of Object.entries(nodes)) {
        const rxLat = node.rx_lat;
        const rxLon = node.rx_lon;
        const polygon = node.empirical_polygon;
        if (rxLat == null || rxLon == null) continue;
        if (!Array.isArray(polygon) || polygon.length < 3) continue;
        let reachKm = 0;
        for (const [vLat, vLon] of polygon) {
          const d = haversineDistanceKm(rxLat, rxLon, vLat, vLon);
          if (d > reachKm) reachKm = d;
        }
        nodeList.push({
          nodeId, rxLat, rxLon, polygon, reachKm,
          latPadDeg: reachKm / 111 + 0.01,
        });
      }

      for (const [hex, trail] of Object.entries(truth)) {
        if (!Array.isArray(trail) || trail.length === 0) continue;
        const last = trail[trail.length - 1];
        if (!last) continue;
        // Coverage membership is tested against the raw ground-truth sample —
        // the freshest real fix. A dead-reckoned position extrapolates the
        // last velocity and, for a stalled/lost track, can glide across a
        // coverage edge and fabricate (or hide) a gap, so it must NOT drive
        // the in/out decision.
        const fixLat = last[0];
        const fixLon = last[1];
        if (fixLat == null || fixLon == null) continue;
        // The drawn endpoint, by contrast, prefers the dead-reckoned position
        // (same source that draws the aircraft dot) so the line's far end
        // lands on the icon rather than lagging behind it by one update.
        const s = smooth[groundTruthKey(hex)];
        const acLat = s ? s.lat : fixLat;
        const acLon = s ? s.lon : fixLon;
        const kmPerDegLon = 111 * Math.cos(fixLat * (Math.PI / 180));

        for (const n of nodeList) {
          // Cheap box reject before any trig.
          if (Math.abs(fixLat - n.rxLat) > n.latPadDeg) continue;
          if (Math.abs(fixLon - n.rxLon) * kmPerDegLon > n.reachKm + 1) continue;
          if (recentDetections[`${hex}|${n.nodeId}`] != null) continue;

          if (!pointInPolygon(fixLat, fixLon, n.polygon)) continue;

          const pairKey = `${hex}|${n.nodeId}`;
          seen.add(pairKey);

          const latLngs = [[n.rxLat, n.rxLon], [acLat, acLon]];
          const existing = polyMap.get(pairKey);
          if (existing) {
            existing.setLatLngs(latLngs);
          } else {
            const line = L.polyline(latLngs, {
              color: "#ef4444", // red-500
              weight: 1.5,
              opacity: 0.7,
              dashArray: "4 6",
              interactive: false,
            });
            line.addTo(map);
            polyMap.set(pairKey, line);
          }
        }
      }

      // Drop polylines whose pair is no longer flagged — either the node
      // started detecting the aircraft, or the aircraft left the beam.
      for (const [pairKey, line] of polyMap) {
        if (seen.has(pairKey)) continue;
        line.remove();
        polyMap.delete(pairKey);
      }
    };

    tick();
    const intervalId = setInterval(tick, 500);
    return () => {
      clearInterval(intervalId);
      for (const line of polyMap.values()) line.remove();
      polyMap.clear();
    };
  }, [map, detectionsRef, groundTruthRef, nodesByIdRef, smoothRef]);

  return null;
});

export default InBeamDiagnostic;
