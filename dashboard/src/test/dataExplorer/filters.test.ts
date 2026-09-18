import { describe, expect, it } from "vitest";

import { inTod, makePredicate } from "../../pages/user/dataExplorer/filters";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { defaultFilters } from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";

/** A file covering the hour that ENDS at the given UTC time. */
function fileEndingAt(iso: string, over: Partial<ArchiveFile> = {}): ArchiveFile {
  const endMs = Date.parse(iso);
  return {
    key: `2026/09/17/ret-a/${iso}.parquet`,
    name: `${iso}.parquet`,
    node: "ret-a",
    day: TODAY,
    size: 1000,
    endMs,
    startMs: endMs - 3_600_000,
    ...over,
  };
}

describe("inTod", () => {
  it("passes everything when the window is the whole day", () => {
    expect(inTod(fileEndingAt("2026-09-17T03:00:00Z"), "00:00", "23:59")).toBe(true);
  });

  it("matches on overlap, not containment", () => {
    // Covers 05:00–06:00. A window of 05:30–07:00 contains neither end.
    const f = fileEndingAt("2026-09-17T06:00:00Z");
    expect(inTod(f, "05:30", "07:00")).toBe(true);
    expect(inTod(f, "04:00", "05:30")).toBe(true);
  });

  it("rejects an hour wholly outside the window", () => {
    const f = fileEndingAt("2026-09-17T06:00:00Z");
    expect(inTod(f, "08:00", "09:00")).toBe(false);
  });

  it("splits an hour that straddles midnight into two spans", () => {
    // Covers 23:30–00:30, so it belongs to both ends of the day.
    const f = fileEndingAt("2026-09-18T00:30:00Z");
    expect(inTod(f, "00:00", "01:00")).toBe(true);
    expect(inTod(f, "23:00", "23:59")).toBe(true);
    expect(inTod(f, "12:00", "13:00")).toBe(false);
    // The window starts inside the file's hour and ends after it, and the
    // reverse at the other end of the day: the span overlaps the window
    // without being contained by it, which containment would reject.
    expect(inTod(f, "00:15", "01:00")).toBe(true);
    expect(inTod(f, "23:00", "23:45")).toBe(true);
  });

  it("wraps a window whose start is later than its end past midnight", () => {
    // 22:00–02:00 is the overnight period, taken from both ends of the day.
    expect(inTod(fileEndingAt("2026-09-17T23:30:00Z"), "22:00", "02:00")).toBe(true);
    expect(inTod(fileEndingAt("2026-09-17T01:00:00Z"), "22:00", "02:00")).toBe(true);
    expect(inTod(fileEndingAt("2026-09-18T00:30:00Z"), "22:00", "02:00")).toBe(true);
    expect(inTod(fileEndingAt("2026-09-17T13:00:00Z"), "22:00", "02:00")).toBe(false);
  });

  it("matches a wrapped window on overlap at either of its edges", () => {
    // Covers 01:30–02:30 and 21:30–22:30: each reaches past one end.
    expect(inTod(fileEndingAt("2026-09-17T02:30:00Z"), "22:00", "02:00")).toBe(true);
    expect(inTod(fileEndingAt("2026-09-17T22:30:00Z"), "22:00", "02:00")).toBe(true);
    // Covers 02:30–03:30 and 20:30–21:30: clear of the window on each side.
    expect(inTod(fileEndingAt("2026-09-17T03:30:00Z"), "22:00", "02:00")).toBe(false);
    expect(inTod(fileEndingAt("2026-09-17T21:30:00Z"), "22:00", "02:00")).toBe(false);
  });
});

describe("makePredicate", () => {
  const eff = new Set(["ret-a", "ret-b"]);

  it("keeps a file whose node is in the effective set", () => {
    const p = makePredicate(defaultFilters(TODAY), eff);
    expect(p(fileEndingAt("2026-09-17T06:00:00Z"))).toBe(true);
  });

  it("drops a file whose node is not", () => {
    const p = makePredicate(defaultFilters(TODAY), eff);
    expect(p(fileEndingAt("2026-09-17T06:00:00Z", { node: "ret-z" }))).toBe(false);
  });

  it("treats the minimum size as a floor, not a threshold to exceed", () => {
    const p = makePredicate({ ...defaultFilters(TODAY), minSize: 1000 }, eff);
    expect(p(fileEndingAt("2026-09-17T06:00:00Z", { size: 1000 }))).toBe(true);
    expect(p(fileEndingAt("2026-09-17T06:00:00Z", { size: 999 }))).toBe(false);
  });

  it("applies the time-of-day window alongside the rest", () => {
    const p = makePredicate({ ...defaultFilters(TODAY), todFrom: "08:00", todTo: "09:00" }, eff);
    expect(p(fileEndingAt("2026-09-17T06:00:00Z"))).toBe(false);
    expect(p(fileEndingAt("2026-09-17T09:00:00Z"))).toBe(true);
  });

  it("applies a window that wraps midnight", () => {
    const p = makePredicate({ ...defaultFilters(TODAY), todFrom: "22:00", todTo: "02:00" }, eff);
    expect(p(fileEndingAt("2026-09-17T01:00:00Z"))).toBe(true);
    expect(p(fileEndingAt("2026-09-17T23:00:00Z"))).toBe(true);
    expect(p(fileEndingAt("2026-09-17T12:00:00Z"))).toBe(false);
  });
});
