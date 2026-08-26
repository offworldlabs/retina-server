/* ── One forget path for the per-object animation stores.
 *
 * LiveAircraftMap keeps eight stores keyed by one string (fixes, smooth,
 * DOM-element cache + its negative cache, Leaflet LatLng cache, two trail
 * buffers, and the marker registry).  Before this module there were THREE
 * hand-maintained prune paths that each knew about a different subset (5, 2
 * and 3 stores respectively) — the exact bug class behind the blue-dot
 * jumping and the never-pruned LatLng cache.  Adding a ninth store now means
 * adding one line to forgetTrack, not remembering three unrelated places. ── */

/** Remove every per-object record for one store key. */
export function forgetTrack(key: string, s) {
  delete s.fixes[key];
  delete s.smooth[key];
  delete s.svgElems[key];
  delete s.svgMiss[key];
  delete s.latLng[key];
  delete s.trails[key];
  delete s.lastTrailSample[key];
  s.markerRegistry?.delete(key);
}

/**
 * Teleport handling: snap the smoothed position to a new fix and drop the
 * motion history, WITHOUT forgetting the object.  Preserves the original
 * guard's semantics exactly — smooth and the cached LatLng are mutated in
 * place (the render loop holds references to them), trails are dropped so
 * the polyline doesn't draw through the discontinuity.
 */
export function snapTrack(key: string, s, lat: number, lon: number, track: number) {
  const sm = s.smooth[key];
  if (sm) {
    sm.lat = lat;
    sm.lon = lon;
  } else {
    s.smooth[key] = { lat, lon, track: track || 0 };
  }
  const cachedLL = s.latLng[key];
  if (cachedLL) {
    cachedLL.lat = lat;
    cachedLL.lng = lon;
  }
  delete s.trails[key];
  delete s.lastTrailSample[key];
}

/**
 * One physical aircraft, one icon across a lane transition.
 *
 * The store key is the feed's `hex`, and that changes when an aircraft moves
 * between lanes: an adsb_single_node entry is keyed by its ICAO hex, a
 * multinode solve by the mn<sha> of its solver key.  The losing side therefore
 * lingers under its old key until the 8 s stale sweep — two icons for one
 * target.  Multi→single is worse: the backend keeps the mn entry alive for
 * 60 s, dead-reckoned, so both are in the SAME frame the moment they drift
 * past backend dedup's 3 km proximity gate.
 *
 * `adsb_assisted` mn entries carry `adsb_hex` (backend: the mn-adsb-<hex> key
 * prefix), which is exactly the ICAO hex the single-node lane keys by — so the
 * pair is identifiable without any geometry.  Per frame, after this frame's
 * fixes are written, resolve every such pair down to the FRESHER solve.
 *
 * Restricted strictly to (multinode fix with adsb_hex X) vs (adsb_single_node
 * fix with hex X): arc-only and truth entries are never touched.
 */
export function reconcileAdsbPairs(aircraft, s, now: number = Date.now()) {
  // Staleness of a stored fix in seconds: its own age-of-solve plus whatever
  // wall time has passed since it was ingested (a WS gap `seen` cannot know
  // about).  Compared against the incoming entry's raw `seen`, which is
  // this-frame-fresh by construction.
  const fixAgeS = (f) => (f.seen ?? 0) + Math.max(0, (now - (f._updatedAt ?? now)) / 1000);

  // adsb_hex → mn store key, from this frame's mn entries first (the common
  // case, already written into fixes above) and then from one linear pass over
  // the stores, which picks up mn losers left over from an EARLIER frame —
  // single→multi→single flaps, where the mn side has stopped arriving but has
  // not yet aged out.  One pass either way; an O(N²) pair scan is what this
  // avoids, and store sizes here are in the hundreds.
  const mnByAdsbHex = new Map<string, string>();
  for (const ac of aircraft) {
    if (ac.position_source !== "multinode_solve" || !ac.adsb_hex) continue;
    if (s.fixes[ac.hex]) mnByAdsbHex.set(ac.adsb_hex, ac.hex);
  }
  for (const key of Object.keys(s.fixes)) {
    const f = s.fixes[key];
    if (f._isTruth) continue;
    if (f.position_source !== "multinode_solve") continue;
    if (!f.adsb_hex || mnByAdsbHex.has(f.adsb_hex)) continue;
    mnByAdsbHex.set(f.adsb_hex, key);
  }
  if (mnByAdsbHex.size === 0) return;

  for (const [adsbHex, mnKey] of mnByAdsbHex) {
    const single = s.fixes[adsbHex];
    if (!single || single._isTruth) continue;
    if (single.position_source !== "adsb_single_node") continue;
    const mn = s.fixes[mnKey];
    if (!mn) continue;
    // Tie goes to the multinode entry: that is backend dedup's own rank.
    if (fixAgeS(single) < fixAgeS(mn)) forgetTrack(mnKey, s);
    else forgetTrack(adsbHex, s);
  }
}

/** Radar entries not seen within staleMs are forgotten (truth-only skipped —
 *  the ground-truth prune/sweep owns those). */
export function sweepStaleRadar(s, now: number, staleMs: number) {
  for (const key of Object.keys(s.fixes)) {
    const f = s.fixes[key];
    if (f._isTruth) continue;
    if (now - (f._updatedAt ?? 0) > staleMs) forgetTrack(key, s);
  }
}
