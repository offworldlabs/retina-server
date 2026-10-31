import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AircraftDetailPanel from "./AircraftDetailPanel";

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

function setup(ac: Record<string, unknown>, archive?: boolean) {
  const view = render(
    <AircraftDetailPanel
      ac={ac}
      onClose={() => {}}
      groundTruth={{}}
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
