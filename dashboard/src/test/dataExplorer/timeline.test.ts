import { describe, expect, it } from "vitest";

import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import {
  clearRange,
  filtersFromFocus,
  filtersFromTemporal,
  mergeIntervals,
  rangeLabel,
  ROW_COLOURS,
  temporalRangeFor,
  timelineRows,
} from "../../pages/user/dataExplorer/timeline";
import { defaultFilters } from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";
const HOUR = 3_600_000;
const at = (iso: string) => Date.parse(iso);

function file(node: string, endIso: string): ArchiveFile {
  const endMs = at(endIso);
  return {
    key: `year=2026/month=09/day=17/node_id=${node}/${endIso}.parquet`,
    name: `${endIso}.parquet`,
    node,
    day: endIso.slice(0, 10),
    size: 1000,
    endMs,
    startMs: endMs - HOUR,
  };
}

const synthByPrefix = (id: string) => id.startsWith("synth-");

describe("mergeIntervals", () => {
  it("joins abutting hours into one bar, whatever order they arrive in", () => {
    const files = [file("a", "2026-09-17T03:00:00Z"), file("a", "2026-09-17T02:00:00Z")];
    expect(mergeIntervals(files)).toEqual([[at("2026-09-17T01:00:00Z"), at("2026-09-17T03:00:00Z")]]);
  });

  it("joins across a gap of up to a minute and no more", () => {
    const joined = [file("a", "2026-09-17T02:00:00Z"), file("a", "2026-09-17T03:01:00Z")];
    expect(mergeIntervals(joined)).toHaveLength(1);
    const split = [file("a", "2026-09-17T02:00:00Z"), file("a", "2026-09-17T03:01:01Z")];
    expect(mergeIntervals(split)).toHaveLength(2);
  });
});

describe("timelineRows", () => {
  const files = [
    file("ret-a", "2026-09-17T02:00:00Z"),
    file("ret-b", "2026-09-17T05:00:00Z"),
    file("synth-c", "2026-09-17T08:00:00Z"),
    file("ret-d", "2026-09-17T11:00:00Z"),
  ];

  it("gives each node its own row up to three", () => {
    const rows = timelineRows(["ret-a", "ret-b", "synth-c"], files, synthByPrefix, true);
    expect(rows.map((r) => r.title)).toEqual(["ret-a", "ret-b", "synth-c"]);
    expect(rows.map((r) => r.color)).toEqual([
      ROW_COLOURS.real,
      ROW_COLOURS.real,
      ROW_COLOURS.synthetic,
    ]);
    expect(rows[0].intervals).toHaveLength(1);
  });

  it("colours nothing as synthetic when no known node is", () => {
    const rows = timelineRows(["synth-c"], files, synthByPrefix, false);
    expect(rows[0].color).toBe(ROW_COLOURS.real);
  });

  it("aggregates past three into one row when no node is synthetic", () => {
    const ids = ["ret-a", "ret-b", "ret-d", "ret-e"];
    const rows = timelineRows(ids, files, () => false, false);
    expect(rows).toHaveLength(1);
    expect(rows[0].title).toBe("4 nodes");
    expect(rows[0].intervals).toHaveLength(3);
  });

  it("splits real from synthetic past three when a synthetic node exists", () => {
    const ids = ["ret-a", "ret-b", "ret-d", "synth-c"];
    const rows = timelineRows(ids, files, synthByPrefix, true);
    expect(rows.map((r) => r.title)).toEqual(["3 real nodes", "1 synth node"]);
    expect(rows[1].intervals).toEqual([[at("2026-09-17T07:00:00Z"), at("2026-09-17T08:00:00Z")]]);
  });

  it("drops the empty side of the split rather than drawing a row with nothing in it", () => {
    const ids = ["ret-a", "ret-b", "ret-d", "ret-e"];
    const rows = timelineRows(ids, files, synthByPrefix, true);
    expect(rows.map((r) => r.id)).toEqual(["real"]);
  });

  it("never draws more than three rows", () => {
    const ids = Array.from({ length: 12 }, (_, i) => (i % 2 ? `synth-${i}` : `ret-${i}`));
    expect(timelineRows(ids, files, synthByPrefix, true).length).toBeLessThanOrEqual(3);
  });
});

describe("the highlighted range", () => {
  it("covers whole days, the last one included", () => {
    const f = { ...defaultFilters(TODAY) };
    expect(temporalRangeFor(f)).toEqual({
      start: at("2026-09-15T00:00:00Z"),
      end: at("2026-09-18T00:00:00Z"),
    });
    expect(rangeLabel(f)).toBe("2026-09-15 → 2026-09-17");
  });

  it("uses the exact times when a time of day is set", () => {
    const f = { ...defaultFilters(TODAY), from: TODAY, todFrom: "06:00", todTo: "09:30" };
    expect(temporalRangeFor(f)).toEqual({
      start: at("2026-09-17T06:00:00Z"),
      end: at("2026-09-17T09:30:00Z"),
    });
    expect(rangeLabel(f)).toBe("2026-09-17 → 2026-09-17 · 06:00–09:30Z");
  });
});

describe("filtersFromTemporal", () => {
  const base = { ...defaultFilters(TODAY), minSize: 2048 };

  it("turns a multi-day drag into whole days, leaving other filters alone", () => {
    const next = filtersFromTemporal(
      { ...base, todFrom: "06:00" },
      { temporalStart: at("2026-09-10T13:00:00Z"), temporalEnd: at("2026-09-12T02:00:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({
      from: "2026-09-10",
      to: "2026-09-12",
      todFrom: "00:00",
      todTo: "23:59",
      minSize: 2048,
    });
  });

  it("treats a range ending on midnight as ending the day before", () => {
    const next = filtersFromTemporal(
      base,
      { temporalStart: at("2026-09-10T00:00:00Z"), temporalEnd: at("2026-09-12T00:00:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({ from: "2026-09-10", to: "2026-09-11" });
  });

  it("keeps the times of a drag within one day as a time-of-day window", () => {
    const next = filtersFromTemporal(
      base,
      { temporalStart: at("2026-09-16T06:00:00Z"), temporalEnd: at("2026-09-16T09:30:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({
      from: "2026-09-16",
      to: "2026-09-16",
      todFrom: "06:00",
      todTo: "09:30",
    });
  });

  it("ends a same-day window running to midnight at the day's last minute", () => {
    const next = filtersFromTemporal(
      base,
      { temporalStart: at("2026-09-16T20:00:00Z"), temporalEnd: at("2026-09-17T00:00:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({ from: "2026-09-16", to: "2026-09-16", todTo: "23:59" });
  });

  it("clamps a drag into the future to today", () => {
    const next = filtersFromTemporal(
      base,
      { temporalStart: at("2026-09-16T00:00:00Z"), temporalEnd: at("2026-09-25T00:00:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({ from: "2026-09-16", to: TODAY });
  });

  it("clears the range when the component reports none", () => {
    const next = filtersFromTemporal({ ...base, from: "2026-09-01", todFrom: "06:00" }, {}, TODAY);
    expect(next).toEqual(clearRange(base, TODAY));
  });
});

describe("filtersFromFocus", () => {
  const base = defaultFilters(TODAY);

  it("focuses a day on that whole day", () => {
    const next = filtersFromFocus(
      base,
      { focusedStart: at("2026-09-12T00:00:00Z"), focusedEnd: at("2026-09-13T00:00:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({ from: "2026-09-12", to: "2026-09-12", todFrom: "00:00", todTo: "23:59" });
  });

  it("focuses an hour as a time-of-day window on its day", () => {
    const next = filtersFromFocus(
      base,
      { focusedStart: at("2026-09-12T14:00:00Z"), focusedEnd: at("2026-09-12T15:00:00Z") },
      TODAY,
    );
    expect(next).toMatchObject({ from: "2026-09-12", to: "2026-09-12", todFrom: "14:00", todTo: "15:00" });
  });

  it("ignores a month, and a focus being cleared", () => {
    const month = { focusedStart: at("2026-08-01T00:00:00Z"), focusedEnd: at("2026-09-01T00:00:00Z") };
    expect(filtersFromFocus(base, month, TODAY)).toBeNull();
    expect(filtersFromFocus(base, {}, TODAY)).toBeNull();
  });
});

describe("clearRange", () => {
  it("resets the dates and time of day and keeps everything else", () => {
    const f = {
      ...defaultFilters(TODAY),
      from: "2026-09-01",
      todFrom: "03:00",
      nodeSel: new Set(["ret-a"]),
      minSize: 1024,
    };
    expect(clearRange(f, TODAY)).toEqual({
      ...defaultFilters(TODAY),
      nodeSel: new Set(["ret-a"]),
      minSize: 1024,
    });
  });
});
