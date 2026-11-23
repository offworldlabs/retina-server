// @ts-nocheck — gradual TS migration
import { memo, useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { dopplerColor } from "./constants";
import { arcGeometry, arcOpacity } from "./arcAfterglow";
import { usePalette } from "./useMapTheme";

/* ── DetectionArcs: imperative Leaflet polylines with timer-driven opacity fade.

      Each arc in the buffer is rendered as its own polyline that fades on
      its own clock — producing a radar-style afterglow trail behind a moving
      target. Each polyline fades linearly to zero over ARC_FADE_MS.  Tick
      interval is 250 ms which is enough resolution for visibly smooth decay
      without React re-render cost.

      Ages are read on the map's clock, `nowMs()`: the wall clock by default,
      the replayed instant in archive mode.  `archived` draws each arc along
      the locus its entry carries (see arcAfterglow.ts). ── */
const DetectionArcs = memo(function DetectionArcs({
  arcsBufferRef, selectedHex, onSelect, onSelectNode, nodesByRefRef, nowMs = Date.now, archived = false,
}) {
  const { DRONE_ARC, SELECTED } = usePalette();
  const map = useMap();
  const polyMapRef = useRef(new Map()); // key → { line: L.polyline, ts, hex, node_ref, ... }
  const onSelectRef = useRef(onSelect);
  const onSelectNodeRef = useRef(onSelectNode);
  const selectedHexRef = useRef(selectedHex);
  // Read inside the tick, so a new clock function rebuilds neither the
  // polylines nor the interval.
  const nowMsRef = useRef(nowMs);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);
  useEffect(() => { onSelectNodeRef.current = onSelectNode; }, [onSelectNode]);
  useEffect(() => { selectedHexRef.current = selectedHex; }, [selectedHex]);
  useEffect(() => { nowMsRef.current = nowMs; }, [nowMs]);

  // `archived` is a dependency: switching between live and archive redraws
  // every polyline, so none keeps a locus built under the other rule.
  useEffect(() => {
    const polyMap = polyMapRef.current;

    const tick = () => {
      const buf = arcsBufferRef.current;
      const now = nowMsRef.current();
      // Lifecycle (arcAfterglow.arcOpacity): ARC_HOLD_MS at full opacity
      // (currently 0 — branch is dormant), then linear fade over
      // ARC_FADE_MS.  Constants live in map/constants.ts so the buffer
      // pruner in hooks.ts shares the same TTL via ARC_TOTAL_LIFE_MS.
      const curSelected = selectedHexRef.current;

      const seen = new Set();

      // Add / update arcs from buffer
      for (const key of Object.keys(buf)) {
        const entry = buf[key];
        // Opacity depends only on the ellipse's own age — not on whether
        // its aircraft is currently selected.  Selection affects colour
        // and stroke weight (below), so the user can still tell which
        // trail belongs to the selection, but each ellipse fades on its
        // own independent clock.
        const opacity = arcOpacity(entry, now);
        if (opacity == null) continue;

        seen.add(key);
        const isSelected = curSelected != null && entry.hex === curSelected;
        // Selected aircraft's arc switches to amber so it stands out from
        // neighbouring doppler-coloured arcs at the same map region — useful
        // in dense areas where cyan/red arcs overlap and the +2 weight bump
        // alone isn't enough to identify which arc belongs to the selected
        // target.
        const color = isSelected
          ? SELECTED
          : (entry.target_class === "drone" ? DRONE_ARC : dopplerColor(entry.doppler_hz ?? 0));
        const weight = isSelected ? 6 : 4;

        const existing = polyMap.get(key);
        if (existing) {
          // Refresh color/weight too: a track can flip target_class
          // (aircraft↔drone) or its doppler band mid-flight, and we want
          // the polyline to reflect the latest classification rather than
          // freezing the colour at first-paint until the arc expires.
          // Geometry is frozen at creation: a bistatic arc is the locus of
          // positions consistent with the delay measured at detection time, so
          // it stays put and fades in place as the aircraft flies on. Only the
          // visual style is refreshed (a track can flip target_class or doppler
          // band mid-flight, and the selected arc switches to amber).
          existing.line.setStyle({ opacity, color, weight });
        } else {
          // First creation: live, the locus is rebuilt from the measured
          // delay and the node registry; archived, it is the one the entry
          // carries (arcAfterglow.arcGeometry says why).
          const arcPoints = arcGeometry(entry, archived, nodesByRefRef?.current);
          const line = L.polyline(arcPoints, {
            color,
            weight,
            opacity,
            lineCap: "round",
            lineJoin: "round",
          });
          line.on("click", (e) => {
            L.DomEvent.stopPropagation(e);
            if (entry.hex) onSelectRef.current?.(entry.hex);
            if (entry.node_ref) onSelectNodeRef.current?.(entry.node_ref);
          });
          line.addTo(map);
          polyMap.set(key, { line, ts: entry.ts, hex: entry.hex, node_ref: entry.node_ref });
        }
      }

      // Remove arcs no longer in seen — past their fade window, stamped
      // after the map's clock, or their buffer entry was pruned.
      for (const [key, info] of polyMap) {
        if (seen.has(key)) continue;
        info.line.remove();
        polyMap.delete(key);
      }
    };

    // Initial tick + interval for smooth updates
    tick();
    const intervalId = setInterval(tick, 250);
    return () => {
      clearInterval(intervalId);
      for (const info of polyMap.values()) info.line.remove();
      polyMap.clear();
    };
  }, [map, arcsBufferRef, nodesByRefRef, archived, DRONE_ARC, SELECTED]);

  return null;
});

export default DetectionArcs;
