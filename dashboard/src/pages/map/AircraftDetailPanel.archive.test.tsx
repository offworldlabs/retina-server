import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AircraftDetailPanel from "./AircraftDetailPanel";
import { groundTruthKey } from "./constants";
import { applyGroundTruthFixes } from "./groundTruthFixes";
import { ADSB, ADSB_CLOCK_MS, GAP_HEX, NO_SOLVE_HEX } from "./replay/__fixtures__/adsb";
import { adsbTraffic, buildAdsbWindow } from "./replay/adsbTraffic";

const at = (iso: string) => Date.parse(iso);
const SOLVED = at("2026-10-18T20:14:05.250Z");

/** A replayed multi-node solve as the synthesiser emits it: the
 *  multinode_to_aircraft field names, ADS-B assisted, with the archive-only
 *  instants on top. */
const replayed = {
  hex: "mn3f2a9c41d0",
  type: "multinode_solve",
  position_source: "multinode_solve",
  multinode: true,
  adsb_assisted: true,
  adsb_hex: "a1b2c3",
  lat: 34.85,
  lon: -82.39,
  alt_m: 10_668,
  n_nodes: 4,
  rms_delay: 0.42,
  rms_doppler: 1.7,
  pos_sigma_m: 120,
  frame_ts_ms: SOLVED,
  solve_ts_ms: SOLVED + 1800,
};

function setup(ac: Record<string, unknown>, archive?: boolean, groundTruth: Record<string, unknown> = {}) {
  const view = render(
    <AircraftDetailPanel
      ac={ac}
      onClose={() => {}}
      groundTruth={groundTruth}
      trails={{}}
      computeError={() => null}
      {...(archive === undefined ? {} : { archive })}
    />,
  );
  /** Label -> value element, in document order. */
  const fields = () =>
    [...view.container.querySelectorAll(".detail-field")].map((f) => ({
      label: f.querySelector(".detail-label")?.textContent,
      value: f.querySelector(".detail-value") as HTMLElement,
    }));
  const field = (label: string) => fields().find((f) => f.label === label)?.value ?? null;
  const section = (title: string) =>
    [...view.container.querySelectorAll(".detail-section")].find(
      (s) => s.querySelector(".detail-section-title")?.textContent === title,
    ) as HTMLElement | undefined;
  return { view, fields, field, section };
}

describe("the detail panel in the archive", () => {
  afterEach(() => vi.restoreAllMocks());

  it("opens the Multi-node section with when the solve was measured, in UTC and local time", () => {
    // 180 minutes east of UTC.
    vi.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(-180);
    const { field, section } = setup(replayed, true);
    const solved = field("Solved");
    expect(solved).toHaveTextContent(/^20:14:05 UTC · local 23:14:05$/);
    expect(solved).toHaveAttribute("title", "2026-10-18T20:14:05.250Z");
    const firstRow = section("Multi-node")?.querySelector(".detail-field .detail-label");
    expect(firstRow).toHaveTextContent("Solved");
  });

  it("still shows what a live solve shows: identity, nodes, altitude, residuals, accuracy", () => {
    const { field } = setup(replayed, true);
    expect(field("HEX")).toHaveTextContent("mn3f2a9c41d0");
    expect(field("Source")).toHaveTextContent("Multi-node (ADS-B assisted)");
    expect(field("Callsign")).toHaveTextContent("—");
    expect(field("Nodes")).toHaveTextContent(/^4$/);
    expect(field("Altitude")).toHaveTextContent("35,000 ft");
    expect(field("RMS Delay")).toHaveTextContent("0.42 μs");
    expect(field("RMS Doppler")).toHaveTextContent("1.7 Hz");
    expect(field("Accuracy")).not.toBeNull();
  });

  it("leaves the row out for an entry without a solve epoch", () => {
    const { field } = setup({ ...replayed, frame_ts_ms: undefined }, true);
    expect(field("Solved")).toBeNull();
    expect(field("Nodes")).not.toBeNull();
  });
});

describe("the detail panel outside the archive", () => {
  it("has no Solved row, even for an entry that carries the epoch", () => {
    for (const archive of [undefined, false]) {
      const { field, view } = setup(replayed, archive);
      expect(field("Solved")).toBeNull();
      view.unmount();
    }
  });

  it("renders the same markup with archive off as with the prop absent", () => {
    const absent = setup(replayed).view.container.innerHTML;
    const off = setup(replayed, false).view.container.innerHTML;
    expect(off).toBe(absent);
    // The one attribute the archive adds: no field value carries it live.
    const live = setup(replayed).view.container;
    expect(live.querySelectorAll(".detail-value[title]")).toHaveLength(0);
  });
});

/** An archived ADS-B aircraft as the ground-truth layer carries it. */
const archivedAdsb = {
  hex: "a1b2c3",
  _isTruth: true,
  lat: 34.85,
  lon: -82.39,
  alt_baro: 35_000,
  gs: 450,
  track: 90,
  speed_ms: 231.5,
  heading: 90,
  has_adsb: true,
  adsb_callsign: "DAL123",
  object_type: "aircraft",
  source: "adsb",
  points: 12,
};

/** An archived ADS-B aircraft as the map holds it once the replayed frame at `clock` is ingested. */
function archivedAt(hex: string, clock: number) {
  const window = buildAdsbWindow(ADSB.traffic, ADSB.traffic.from, ADSB.traffic.to);
  const { ground_truth, ground_truth_meta } = adsbTraffic([window], clock);
  const fixes: Record<string, Record<string, unknown>> = {};
  applyGroundTruthFixes(fixes, ground_truth, ground_truth_meta, clock);
  return { ac: fixes[groundTruthKey(hex)], groundTruth: ground_truth };
}

describe("the detail panel for an archived ADS-B aircraft", () => {
  afterEach(() => vi.restoreAllMocks());

  it("says when the shown fix was received, in UTC and local time, and how old it is at the replay clock", () => {
    // 180 minutes east of UTC.
    vi.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(-180);
    // adbeaa at 19:59:25Z: its latest snapshot, 19:59:10Z, still holds the fix received at 19:58:26Z.
    const stale = archivedAt(GAP_HEX, Date.UTC(2026, 9, 18, 19, 59, 25));
    const old = setup(stale.ac, true, stale.groundTruth);
    expect(old.field("Fix received")).toHaveTextContent(/^19:58:26 UTC · local 22:58:26$/);
    expect(old.field("Fix received")).toHaveAttribute("title", "2026-10-18T19:58:26.000Z");
    expect(old.field("Fix age")).toHaveTextContent(/^59 s$/);
    expect(old.section("ADS-B snapshot")?.textContent).toContain("Fix age59 s");
    old.view.unmount();

    // a07caf at 20:00:00Z: the snapshot of the instant, its fix received a second before.
    const fresh = archivedAt(NO_SOLVE_HEX, ADSB_CLOCK_MS);
    const current = setup(fresh.ac, true, fresh.groundTruth);
    expect(current.field("Fix received")).toHaveTextContent(/^19:59:59 UTC · local 22:59:59$/);
    expect(current.field("Fix age")).toHaveTextContent(/^1 s$/);
  });

  it("reads as an archived ADS-B snapshot, with no simulation wording", () => {
    const { field, section, view } = setup(archivedAdsb, true);
    expect(section("ADS-B snapshot")).toBeDefined();
    expect(field("Source")).toHaveTextContent(/^Archived ADS-B snapshot$/);
    expect(field("Callsign")).toHaveTextContent(/^DAL123$/);
    expect(field("Speed")).toHaveTextContent("450 kts (232 m/s)");
    expect(field("Heading")).toHaveTextContent(/^90°$/);
    expect(field("ADS-B pts")).toHaveTextContent(/^0$/);
    expect(section("Simulation (debug)")).toBeUndefined();
    expect(view.container.textContent).not.toMatch(/Simulat|Ground truth|Truth pts|debug/i);
  });

  it("shows a dash for a speed and heading the snapshot row did not have", () => {
    const { field } = setup(
      { ...archivedAdsb, gs: 0, track: 0, speed_ms: undefined, heading: undefined },
      true,
    );
    expect(field("Speed")).toHaveTextContent(/^—$/);
    expect(field("Heading")).toHaveTextContent(/^—$/);
  });

  it("keeps a real zero speed and a due-north heading", () => {
    const { field } = setup({ ...archivedAdsb, gs: 0, track: 0, speed_ms: 0, heading: 0 }, true);
    expect(field("Speed")).toHaveTextContent("0 kts (0 m/s)");
    expect(field("Heading")).toHaveTextContent(/^0°$/);
  });
});

describe("the detail panel for live ground truth", () => {
  it.each([
    ["live", "Live ADS-B feed (real aircraft, echoed by synthetic nodes)"],
    ["sim", "Simulated spawn"],
  ])("keeps the simulation section for source %s", (source, text) => {
    const { field, section } = setup({ ...archivedAdsb, source, seen_ms: 1, _updatedAt: 2 }, false);
    expect(section("Simulation (debug)")).toBeDefined();
    expect(section("ADS-B snapshot")).toBeUndefined();
    expect(field("Source")).toHaveTextContent(text);
    expect(field("Status")).toHaveTextContent("Ground truth only");
    expect(field("Truth pts")).toHaveTextContent(/^0$/);
    expect(field("Fix received")).toBeNull();
    expect(field("Fix age")).toBeNull();
  });
});
