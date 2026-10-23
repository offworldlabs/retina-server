import { GT_PRUNE_GRACE_MS, groundTruthKey } from "./constants";
import { forgetTrack } from "./trackStores";
import { mToFt, msToKnots } from "./units";

/* ── Ground-truth objects in the frontend animation stores.
 *
 * The map keeps four per-object stores — fixesRef (server fix + anchor time),
 * smoothRef (60 fps dead-reckoned position), and two trail buffers — all keyed
 * by one string.  Radar tracks key on their ICAO hex.  In simulation a radar
 * track carries the *same* hex as the aircraft that produced it, so if truth
 * used the bare hex too, the radar ingest and the truth ingest would write one
 * key and the marker would alternate between the solved position and the true
 * one on every 1 Hz update — measured 29.8 km apart for a single-node arc
 * track on staging, which is what "the blue dots jump around" was.
 *
 * Truth therefore lives under a namespaced key while `hex` on the object stays
 * the real hex, so labels, selection and error computation are unaffected.
 * `_key` is the store key and is what the animation loop must index by. ── */

/**
 * Fold a ground-truth snapshot into `fixes` in place.
 *
 * @param fixes   the fixesRef store, mutated
 * @param snapshot  hex → trail, each point [lat, lon, alt_m, ts]; alt_m null
 *                  when the fix had no altitude, which leaves alt_baro unset
 *                  (an "adsb" meta with null speed_ms / heading leaves gs / track unset)
 * @param meta      hex → { speed_ms, heading, object_type, is_anomalous, has_adsb, source };
 *                  an archived ADS-B one also has seen_ms, when its shown fix was received
 * @param now       ms epoch used as the dead-reckoning anchor; the map's clock, which an
 *                  entry keeps as _updatedAt (the replayed frame's instant in the archive)
 * @returns the set of store keys this snapshot vouches for
 */
export function applyGroundTruthFixes(fixes, snapshot, meta, now) {
  const activeKeys = new Set();
  for (const [hex, positions] of Object.entries(snapshot || {})) {
    if (!Array.isArray(positions) || positions.length === 0) continue;
    const last = positions[positions.length - 1];
    const m = (meta || {})[hex] || {};
    const lat = last[0], lon = last[1];
    const key = groundTruthKey(hex);
    activeKeys.add(key);
    const prev = fixes[key];
    // Only re-anchor when the fix actually moved; otherwise dead-reckoning
    // would restart from zero elapsed on every re-broadcast and the object
    // would stall between server updates.
    const posChanged = !prev || prev._fixLat !== lat || prev._fixLon !== lon;
    // An archived ADS-B row can lack gs/track; unknown is not 0 kt / 0° due
    // north. Live sources keep the `|| 0` default.
    const archivedAdsb = m.source === "adsb";
    fixes[key] = {
      hex,
      _key: key,
      lat, lon,
      // A missing altitude stays missing: 0 ft would read as on the ground.
      alt_baro: Number.isFinite(last[2]) ? Math.round(mToFt(last[2])) : undefined,
      gs: archivedAdsb && m.speed_ms == null
        ? undefined
        : Math.round(msToKnots(m.speed_ms || 0) * 10) / 10,
      track: archivedAdsb && m.heading == null ? undefined : m.heading || 0,
      speed_ms: m.speed_ms,
      heading: m.heading,
      object_type: m.object_type,
      is_anomalous: m.is_anomalous,
      // Simulated parameters for the debug detail panel.  "Dark" objects are
      // object_type "aircraft" with has_adsb false.
      has_adsb: m.has_adsb,
      // Provenance: "live" = mirrored from the ADS-B feed, "sim" = spawned.
      // Colours the dot (map/truthColor) and labels the detail panel.
      source: m.source,
      adsb_callsign: m.adsb_callsign,
      // Archived ADS-B: when the shown fix was received, for its age at _updatedAt.
      seen_ms: m.seen_ms,
      anomaly_event: m.anomaly_event,
      points: positions.length,
      _isTruth: true,
      _fixLat: lat,
      _fixLon: lon,
      _fixTs: posChanged ? now : (prev?._fixTs ?? now),
      _updatedAt: now,
    };
  }
  return activeKeys;
}

/**
 * Drop truth entries the latest snapshot no longer vouches for, from every
 * per-object store.  Radar entries are left alone — they have their own
 * staleness rule (trackStores.sweepStaleRadar).
 *
 * Takes the trackStores bundle rather than varargs companions: the varargs
 * form made a forgotten store silent, which is the exact bug class the
 * bundle exists to close.
 *
 * A key missing from ONE snapshot is not evidence the object despawned: the
 * backend rebuilds the GT snapshot only every 5 s while its trail GC runs on
 * a 10 s rule, so a push hiccup drops a hex for a few snapshots and brings it
 * back.  Forgetting on first absence made the dot blink out and reappear.
 * Instead the entry is kept (dead-reckoning carries it) until it has gone
 * unvouched for `graceMs` — `_updatedAt` is only refreshed for vouched keys,
 * so it ages naturally while the key is absent.
 */
export function pruneGroundTruthFixes(stores, activeKeys, now, graceMs) {
  for (const key of Object.keys(stores.fixes)) {
    const f = stores.fixes[key];
    if (!f._isTruth) continue;
    if (activeKeys.has(key)) continue;
    if (now - (f._updatedAt ?? 0) <= graceMs) continue;
    forgetTrack(key, stores);
  }
}

/**
 * How long a truth entry a snapshot no longer vouches for is kept
 * (pruneGroundTruthFixes). Live, GT_PRUNE_GRACE_MS, for the push hiccups
 * described there. In archive replay, none: a replayed frame is the
 * archive's whole answer for its instant, and it already keeps an aircraft
 * for two snapshot periods after its last fix (replay/adsbTraffic.ts), so an
 * aircraft it leaves out is gone at that instant.
 */
export function groundTruthPruneGraceMs(archive: boolean): number {
  return archive ? 0 : GT_PRUNE_GRACE_MS;
}

/**
 * Time-based sweep for when the ground-truth FEED stops entirely.
 *
 * pruneGroundTruthFixes only runs when a snapshot *arrives*; if the WS drops
 * or the backend stops emitting ground_truth, no snapshot ever revokes the
 * old keys and every truth object keeps dead-reckoning forever — stale blue
 * dots flying indefinitely.  Radar tracks have STALE_AIRCRAFT_MS; this is
 * the equivalent for truth, driven from the render loop so it runs even when
 * no data is arriving.
 */
export function sweepStaleGroundTruthFixes(stores, now, ttlMs) {
  for (const key of Object.keys(stores.fixes)) {
    const f = stores.fixes[key];
    if (!f._isTruth) continue;
    if (now - (f._updatedAt ?? 0) > ttlMs) forgetTrack(key, stores);
  }
}
