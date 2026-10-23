/* ------------------------------------------------------------------ */
/*  The ADS-B layer's realistic window: the route's widest, 60        */
/*  minutes, centred on generate500's bench instant (2026-10-18       */
/*  [13:45Z, 14:45Z)), as dense as the seeded day: about 150          */
/*  aircraft in each 10 s snapshot, so about 54,000 rows.             */
/*  Deterministic.                                                    */
/*                                                                    */
/*  Shaped from 2000z-adsb.json: its fields and their types, its      */
/*  share of rows without a callsign, positions to six decimals as    */
/*  the background traffic's, whole feet and one-decimal speeds and   */
/*  tracks, fixes received 1-8 s before their snapshot (whole         */
/*  seconds, as served). Each aircraft flies straight for its stretch */
/*  of the hour within 260 km of Greenville, and stretches overlap    */
/*  the window's edges as real traffic does.                          */
/* ------------------------------------------------------------------ */

import type { AdsbResponse, AdsbRow } from "../types";
import { ADSB } from "./adsb";

export const ADSB_WINDOW_FROM = Date.UTC(2026, 9, 18, 13, 45, 0);
export const ADSB_WINDOW_TO = Date.UTC(2026, 9, 18, 14, 45, 0);
/** The aircraft over the hour; each is seen for 20 minutes, so about a quarter of them at once. */
export const GENERATED_AIRCRAFT = 600;
const STRETCH_MS = 20 * 60_000;
const SNAPSHOT_MS = 10_000;
const GREENVILLE = { lat: 34.85, lon: -82.39 };
const M_PER_DEG = 111_320;

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const round = (x: number, d: number) => Math.round(x * 10 ** d) / 10 ** d;

/** About 54,000 snapshot rows in the route's order (ts_ms, then hex). */
export function generateAdsb(seed = 150): AdsbResponse {
  const rand = mulberry32(seed);
  // Fix ages off a stream of their own, so the traffic is the one it was.
  const ages = mulberry32(seed + 1);
  const real = ADSB.traffic.rows;
  const noCallsign = real.filter((r) => r.flight === null).length / real.length;
  const airlines = ["AAL", "DAL", "UAL", "SWA", "EDV", "JIA", "RPA", "SKW"];

  const rows: AdsbRow[] = [];
  const span = ADSB_WINDOW_TO - ADSB_WINDOW_FROM;
  for (let a = 0; a < GENERATED_AIRCRAFT; a++) {
    const hex = (0xa00001 + a * 4099).toString(16);
    const start = ADSB_WINDOW_FROM - STRETCH_MS + Math.floor(rand() * (span + STRETCH_MS) / SNAPSHOT_MS) * SNAPSHOT_MS;
    const r = 260_000 * Math.sqrt(rand());
    const bearing = rand() * 2 * Math.PI;
    let lat = GREENVILLE.lat + (r * Math.cos(bearing)) / M_PER_DEG;
    let lon = GREENVILLE.lon + (r * Math.sin(bearing)) / (M_PER_DEG * Math.cos((GREENVILLE.lat * Math.PI) / 180));
    const track = rand() * 360;
    const gs = 90 + rand() * 390;
    const alt = Math.round((1_500 + rand() * 38_500) / 25) * 25;
    const flight = rand() < noCallsign ? null : `${airlines[a % airlines.length]}${100 + Math.floor(rand() * 8900)}`;
    const step = (gs * 0.514444 * SNAPSHOT_MS) / 1000;
    const dLat = (step * Math.cos((track * Math.PI) / 180)) / M_PER_DEG;
    for (let ts = start; ts < start + STRETCH_MS; ts += SNAPSHOT_MS) {
      const dLon = (step * Math.sin((track * Math.PI) / 180)) / (M_PER_DEG * Math.cos((lat * Math.PI) / 180));
      lat += dLat;
      lon += dLon;
      if (ts < ADSB_WINDOW_FROM || ts >= ADSB_WINDOW_TO) continue;
      rows.push({
        ts_ms: ts,
        hex,
        lat: round(lat, 6),
        lon: round(lon, 6),
        alt_baro: alt + Math.round((rand() - 0.5) * 4) * 25,
        gs: round(gs + (rand() - 0.5) * 6, 1),
        track: round(track + (rand() - 0.5) * 2, 1),
        flight,
        seen_ms: ts - 1000 * (1 + Math.floor(ages() * 8)),
      });
    }
  }
  rows.sort((x, y) => x.ts_ms - y.ts_ms || (x.hex < y.hex ? -1 : x.hex > y.hex ? 1 : 0));
  return { from: ADSB_WINDOW_FROM, to: ADSB_WINDOW_TO, fleet: "real", count: rows.length, truncated: false, rows };
}
