import { describe, it, expect } from "vitest";
import { bearingDeg, buildBistaticArc, type NodeGeometry } from "./bistaticArc";

const C_KM_PER_US = 0.299792458;

// RX/TX ~18.5 km apart, north-pointing 42° beam wedge.
const NODE: NodeGeometry = {
  rx_lat: 34.0,
  rx_lon: -82.0,
  tx_lat: 34.0,
  tx_lon: -81.8,
  beam_azimuth_deg: 0,
  beam_width_deg: 42,
  max_range_km: 60,
  max_bistatic_range_km: null,
};

/**
 * Recompute the builder's own ENU differential for a returned (lat, lon)
 * point — mirrors the internal projection (equirect; tx offsets use the
 * midpoint latitude, point inversion uses the RX latitude per enuToLla).
 */
function differentialAt(node: NodeGeometry, lat: number, lon: number): number {
  const rxLat = node.rx_lat;
  const rxLon = node.rx_lon;
  const cosMid = Math.max(
    0.1,
    Math.cos((((rxLat + node.tx_lat) / 2) * Math.PI) / 180),
  );
  const txEast = (node.tx_lon - rxLon) * 111.32 * cosMid;
  const txNorth = (node.tx_lat - rxLat) * 111.32;
  const baseline = Math.hypot(txEast, txNorth);
  const cosRx = Math.max(0.1, Math.cos((rxLat * Math.PI) / 180));
  const east = (lon - rxLon) * 111.32 * cosRx;
  const north = (lat - rxLat) * 111.32;
  const g = Math.hypot(east, north);
  const gTx = Math.hypot(east - txEast, north - txNorth);
  return g + gTx - baseline;
}

describe("buildBistaticArc (2D, no altitude)", () => {
  it("returns a locus whose 2D differential matches the measured delay", () => {
    const delayUs = 46.2;
    const arc = buildBistaticArc(delayUs, NODE);
    expect(arc).not.toBeNull();
    expect(arc!.length).toBeGreaterThanOrEqual(2);
    const target = delayUs * C_KM_PER_US;
    for (const [lat, lon] of arc!) {
      expect(differentialAt(NODE, lat, lon)).toBeCloseTo(target, 2);
    }
  });

  it("rejects non-positive delays and missing geometry", () => {
    expect(buildBistaticArc(0, NODE)).toBeNull();
    expect(buildBistaticArc(-5, NODE)).toBeNull();
    expect(
      buildBistaticArc(46.2, { ...NODE, tx_lat: null as any }),
    ).toBeNull();
  });

  it("returns null when the delay exceeds a declared differential limit", () => {
    const node = { ...NODE, max_bistatic_range_km: 10 };
    // 46.2 µs → 13.85 km differential > 10 km limit
    expect(buildBistaticArc(46.2, node)).toBeNull();
  });

  it("ignores altitude-ish fields on the node — the locus is unchanged", () => {
    // rx_alt_m / tx_alt_m stay on NodeGeometry for other consumers (see
    // hooks.ts / types.ts) but no longer feed this builder's geometry
    // (2026-08 direction: arcs are altitude-agnostic by design).
    const delayUs = 46.2;
    const plain = buildBistaticArc(delayUs, NODE);
    const withAlt = buildBistaticArc(delayUs, {
      ...NODE, rx_alt_m: 300, tx_alt_m: 9500,
    });
    expect(withAlt).toEqual(plain);
  });
});

/**
 * The locus by per-bearing binary search of the differential, as
 * backend/services/track_gates.py::_build_single_node_arc solves it: the
 * reference the closed-form builder is held to.
 */
function bisectionArc(delayUs: number, node: NodeGeometry): [number, number][] | null {
  if (!delayUs || delayUs <= 0) return null;
  const { rx_lat, rx_lon, tx_lat, tx_lon } = node;
  const beamWidthDeg = Number(node.beam_width_deg ?? 42);
  const maxRangeKm = Number(node.max_range_km ?? 50);
  const beamAzimuthDeg = node.beam_azimuth_deg ?? (bearingDeg(rx_lat, rx_lon, tx_lat, tx_lon) + 90) % 360;
  const cosMid = Math.max(0.1, Math.cos((((rx_lat + tx_lat) / 2) * Math.PI) / 180));
  const txEast = (tx_lon - rx_lon) * 111.32 * cosMid;
  const txNorth = (tx_lat - rx_lat) * 111.32;
  const baseline = Math.hypot(txEast, txNorth);
  const differential = delayUs * C_KM_PER_US;
  const differentialAt = (range: number, bearing: number) => {
    const rad = (bearing * Math.PI) / 180;
    return Math.hypot(Math.sin(rad) * range - txEast, Math.cos(rad) * range - txNorth) + range - baseline;
  };
  let ceiling = maxRangeKm;
  const limit = node.max_bistatic_range_km;
  if (limit != null && Number.isFinite(limit)) {
    if (differential > Number(limit)) return null;
    ceiling = differential / 2 + baseline;
  }
  const sweep = beamWidthDeg >= 360 ? 360 : beamWidthDeg;
  const steps = beamWidthDeg >= 360 ? 72 : 36;
  const cosRx = Math.max(0.1, Math.cos((rx_lat * Math.PI) / 180));
  const points: [number, number][] = [];
  for (let step = 0; step <= steps; step++) {
    const bearing = beamAzimuthDeg - sweep / 2 + sweep * (step / steps);
    let lo = 0;
    let hi = ceiling;
    if (differentialAt(hi, bearing) < differential) continue;
    for (let i = 0; i < 32; i++) {
      const mid = (lo + hi) / 2;
      if (differentialAt(mid, bearing) < differential) lo = mid;
      else hi = mid;
    }
    const rad = (bearing * Math.PI) / 180;
    points.push([rx_lat + (hi * Math.cos(rad)) / 111.32, rx_lon + (hi * Math.sin(rad)) / (111.32 * cosRx)]);
  }
  return points.length < 2 ? null : points;
}

describe("buildBistaticArc against a per-bearing bisection of the differential", () => {
  it("keeps the same vertices and empty cases, every point within a metre", () => {
    const RANGE_KM = 50;
    const LIMIT_KM = 120;
    const beams: Partial<NodeGeometry>[] = [
      { beam_width_deg: 360, beam_azimuth_deg: null }, // omnidirectional, sweep centred broadside
      { beam_width_deg: null, beam_azimuth_deg: null }, // the default 42-degree broadside wedge
      { beam_width_deg: 42, beam_azimuth_deg: 0 }, // centred on true north, where a due-north TX sits
      { beam_width_deg: 10, beam_azimuth_deg: 300 },
    ];
    const clips: Partial<NodeGeometry>[] = [
      { max_range_km: RANGE_KM, max_bistatic_range_km: null },
      { max_range_km: 1000, max_bistatic_range_km: null }, // no clip for any delay below
      { max_range_km: RANGE_KM, max_bistatic_range_km: LIMIT_KM },
    ];

    let cases = 0;
    let full = 0;
    let clipped = 0;
    let empty = 0;
    let worstM = 0;
    const mismatches: string[] = [];
    for (const rxLat of [-60, -40, -20, 0, 20, 40, 60]) {
      for (const baselineKm of [5, 25, 70, 150]) {
        for (const txDirDeg of [0, 123, 250]) {
          const dir = (txDirDeg * Math.PI) / 180;
          const rx_lon = 30.5;
          const tx_lat = rxLat + (baselineKm * Math.cos(dir)) / 111.32;
          const tx_lon = rx_lon + (baselineKm * Math.sin(dir)) / (111.32 * Math.cos((rxLat * Math.PI) / 180));
          for (const beam of beams) {
            for (const clip of clips) {
              const node: NodeGeometry = { rx_lat: rxLat, rx_lon, tx_lat, tx_lon, ...beam, ...clip };
              const bounded = clip.max_bistatic_range_km != null;
              // Differentials (km): near zero, inside the area, and either side of
              // where the range circle starts and ends clipping, or of the limit.
              const near = bounded
                ? [LIMIT_KM - 0.001, LIMIT_KM + 0.001]
                : [2 * (clip.max_range_km! - baselineKm), 2 * clip.max_range_km! - baselineKm, 2 * clip.max_range_km!]
                    .flatMap((d) => [d - 0.01, d + 0.01])
                    .filter((d) => d > 0);
              for (const differentialKm of [0.0003, 0.03, 0.6, 7.5, 36, ...near]) {
                const delayUs = differentialKm / C_KM_PER_US;
                const expected = bisectionArc(delayUs, node);
                const actual = buildBistaticArc(delayUs, node);
                const label = JSON.stringify({ delayUs, ...node });
                cases++;
                if (expected === null || actual === null) {
                  if (expected !== actual) mismatches.push(`${label}: ${expected?.length ?? "null"} vs ${actual?.length ?? "null"}`);
                  else empty++;
                  continue;
                }
                if (actual.length !== expected.length) {
                  mismatches.push(`${label}: ${expected.length} vs ${actual.length} vertices`);
                  continue;
                }
                const steps = Number(node.beam_width_deg ?? 42) >= 360 ? 72 : 36;
                if (actual.length === steps + 1) full++;
                else clipped++;
                const eastMPerDeg = 111_320 * Math.max(0.1, Math.cos((rxLat * Math.PI) / 180));
                expected.forEach(([lat, lon], i) => {
                  const m = Math.hypot((actual[i][0] - lat) * 111_320, (actual[i][1] - lon) * eastMPerDeg);
                  worstM = Math.max(worstM, m);
                  if (!(m < 1)) mismatches.push(`${label}: vertex ${i} off by ${m.toFixed(3)} m`);
                });
              }
            }
          }
        }
      }
    }
    console.info(
      `closed-form locus vs bisection: ${cases} cases, ${full} full arcs, ${clipped} clipped, ${empty} without a locus; ` +
        `worst vertex ${worstM.toExponential(2)} m`,
    );
    expect(mismatches.slice(0, 20)).toEqual([]);
    expect(worstM).toBeLessThan(1);
    // The grid reaches every outcome: whole arcs, arcs the area clips, and no locus at all.
    expect(full).toBeGreaterThan(100);
    expect(clipped).toBeGreaterThan(100);
    expect(empty).toBeGreaterThan(100);
  });
});
