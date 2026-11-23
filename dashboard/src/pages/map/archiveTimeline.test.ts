import { describe, expect, it } from "vitest";

import {
  availabilityRows,
  formatLocalBeside,
  formatLocalClock,
  formatUtcClock,
  formatUtcHm,
  formatUtcInput,
  formatUtcOffset,
  HOUR_MS,
  instantFromSpan,
  mergeIntervals,
  parseUtcInput,
} from "./archiveTimeline";

const at = (iso: string) => Date.parse(iso);
const H = (iso: string, count: number, unclassified = 0) => ({ start_ms: at(iso), count, unclassified });

describe("availabilityRows", () => {
  it("joins contiguous hours with solves into one interval, and leaves a gap where an hour is missing", () => {
    const rows = availabilityRows([
      H("2026-10-18T20:00:00Z", 12),
      H("2026-10-18T18:00:00Z", 3), // out of order on purpose
      H("2026-10-18T19:00:00Z", 7),
      // 21:00 missing from the list: a gap
      H("2026-10-18T22:00:00Z", 1),
    ]);
    expect(rows).toEqual([
      {
        id: "solves",
        title: "Solves",
        color: "var(--accent)",
        intervals: [
          [at("2026-10-18T18:00:00Z"), at("2026-10-18T21:00:00Z")],
          [at("2026-10-18T22:00:00Z"), at("2026-10-18T23:00:00Z")],
        ],
      },
    ]);
  });

  it("treats an hour with neither solves nor unclassified rows as a gap", () => {
    const rows = availabilityRows([
      H("2026-10-18T18:00:00Z", 4),
      H("2026-10-18T19:00:00Z", 0, 0),
      H("2026-10-18T20:00:00Z", 4),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].intervals).toEqual([
      [at("2026-10-18T18:00:00Z"), at("2026-10-18T19:00:00Z")],
      [at("2026-10-18T20:00:00Z"), at("2026-10-18T21:00:00Z")],
    ]);
  });

  it("puts unclassified rows on their own row, including an hour that holds nothing else", () => {
    const rows = availabilityRows([
      H("2026-10-18T18:00:00Z", 5, 2),
      H("2026-10-18T19:00:00Z", 0, 9), // unclassified only
      H("2026-10-18T20:00:00Z", 6, 0),
    ]);
    expect(rows.map((r) => r.id)).toEqual(["solves", "unclassified"]);
    expect(rows[0].intervals).toEqual([
      [at("2026-10-18T18:00:00Z"), at("2026-10-18T19:00:00Z")],
      [at("2026-10-18T20:00:00Z"), at("2026-10-18T21:00:00Z")],
    ]);
    expect(rows[1]).toEqual({
      id: "unclassified",
      title: "Unclassified (fleet unknown)",
      color: "var(--warning)",
      intervals: [[at("2026-10-18T18:00:00Z"), at("2026-10-18T20:00:00Z")]],
    });
  });

  it("keeps the solves row, empty, and drops the unclassified one when there is no data at all", () => {
    expect(availabilityRows([])).toEqual([
      { id: "solves", title: "Solves", color: "var(--accent)", intervals: [] },
    ]);
  });
});

describe("mergeIntervals", () => {
  it("joins touching and overlapping intervals and keeps separate ones apart", () => {
    expect(
      mergeIntervals([
        [5 * HOUR_MS, 6 * HOUR_MS],
        [0, HOUR_MS],
        [HOUR_MS, 2 * HOUR_MS],
        [HOUR_MS + 1, HOUR_MS + 2],
      ]),
    ).toEqual([
      [0, 2 * HOUR_MS],
      [5 * HOUR_MS, 6 * HOUR_MS],
    ]);
  });
});

describe("instantFromSpan", () => {
  const NOW = at("2026-10-19T12:00:00Z");

  it("names the middle of the span, rounded to the minute", () => {
    expect(instantFromSpan(at("2026-10-18T20:00:00Z"), at("2026-10-18T21:00:00Z"), NOW)).toBe(
      at("2026-10-18T20:30:00Z"),
    );
    expect(instantFromSpan(at("2026-10-18T20:00:00Z"), at("2026-10-18T20:01:50Z"), NOW)).toBe(
      at("2026-10-18T20:01:00Z"),
    );
  });

  it("never names an instant past now", () => {
    expect(instantFromSpan(at("2026-10-19T11:00:00Z"), at("2026-10-19T14:00:00Z"), NOW)).toBe(NOW);
  });

  it("names nothing for a gesture without a span", () => {
    expect(instantFromSpan(undefined, at("2026-10-18T21:00:00Z"), NOW)).toBeNull();
    expect(instantFromSpan(at("2026-10-18T21:00:00Z"), null, NOW)).toBeNull();
  });
});

describe("the UTC formatters", () => {
  const T = at("2026-10-18T20:14:05.789Z");

  it("give a datetime-local value, the clock and the hour-minute in UTC, floored to the second", () => {
    expect(formatUtcInput(T)).toBe("2026-10-18T20:14:05");
    expect(formatUtcClock(T)).toBe("20:14:05");
    expect(formatUtcHm(T)).toBe("20:14");
  });

  it("give nothing for a non-instant", () => {
    expect(formatUtcInput(NaN)).toBe("");
    expect(formatUtcClock(Infinity)).toBe("");
  });
});

describe("parseUtcInput", () => {
  it("reads the field as UTC, with or without seconds and milliseconds", () => {
    expect(parseUtcInput("2026-10-18T20:14:05")).toBe(at("2026-10-18T20:14:05Z"));
    expect(parseUtcInput("2026-10-18T20:14")).toBe(at("2026-10-18T20:14:00Z"));
    expect(parseUtcInput("2026-10-18T20:14:05.000")).toBe(at("2026-10-18T20:14:05Z"));
    expect(parseUtcInput("2026-10-18T20:14:05.5")).toBe(at("2026-10-18T20:14:05.500Z"));
    expect(parseUtcInput("2026-10-18 20:14:05")).toBe(at("2026-10-18T20:14:05Z"));
  });

  it("round-trips what the formatter writes", () => {
    const T = at("2026-02-28T23:59:59Z");
    expect(parseUtcInput(formatUtcInput(T))).toBe(T);
  });

  it("rejects an empty, half-typed or impossible value", () => {
    for (const bad of [
      "",
      "2026-10-18",
      "2026-10-18T",
      "2026-10-18T25:00",
      "2026-10-18T20:60",
      "2026-10-18T20:14:61",
      "2026-13-01T00:00",
      "2026-02-30T12:00",
      "2026-10-18T20:14Z",
      "yesterday",
    ]) {
      expect(parseUtcInput(bad), bad).toBeNull();
    }
  });
});

describe("the local reading beside the field", () => {
  const T = at("2026-10-18T20:14:05Z");

  it("names the local wall clock and the offset, whole hours without minutes", () => {
    expect(formatLocalBeside(T, 180)).toBe("local 23:14, UTC+3");
    expect(formatLocalBeside(T, -240)).toBe("local 16:14, UTC-4");
    expect(formatLocalBeside(T, 0)).toBe("local 20:14, UTC+0");
  });

  it("carries a part-hour offset as minutes, across midnight", () => {
    expect(formatLocalBeside(T, 330)).toBe("local 01:44, UTC+5:30");
    expect(formatUtcOffset(-570)).toBe("UTC-9:30");
    expect(formatUtcOffset(345)).toBe("UTC+5:45");
  });

  it("defaults to the browser's offset at that instant", () => {
    const off = -new Date(T).getTimezoneOffset();
    expect(formatLocalBeside(T)).toBe(formatLocalBeside(T, off));
  });
});

describe("the local clock", () => {
  const T = at("2026-10-18T20:14:05Z");

  it("reads the wall clock east and west of UTC, seconds included", () => {
    expect(formatLocalClock(T, 180)).toBe("23:14:05");
    expect(formatLocalClock(T, -300)).toBe("15:14:05");
    expect(formatLocalClock(T, 0)).toBe("20:14:05");
  });

  it("rolls over midnight in both directions", () => {
    expect(formatLocalClock(at("2026-10-18T22:30:15Z"), 180)).toBe("01:30:15");
    expect(formatLocalClock(at("2026-10-18T02:10:00Z"), -300)).toBe("21:10:00");
  });

  it("is empty for a non-finite instant", () => {
    expect(formatLocalClock(Infinity, 180)).toBe("");
  });

  it("defaults to the browser's offset at that instant", () => {
    const off = -new Date(T).getTimezoneOffset();
    expect(formatLocalClock(T)).toBe(formatLocalClock(T, off));
  });
});
