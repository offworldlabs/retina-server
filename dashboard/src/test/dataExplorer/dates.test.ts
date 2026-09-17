import { afterEach, describe, expect, it, vi } from "vitest";

import {
  addDays,
  DAY_MS,
  daysBetween,
  hhmm,
  isoDay,
  MAX_RANGE_DAYS,
  minuteOfDay,
  todayUTC,
} from "../../pages/user/dataExplorer/dates";

afterEach(() => {
  vi.useRealTimers();
});

describe("isoDay and todayUTC", () => {
  it("reads the UTC day, not the local one", () => {
    // 23:30 UTC on the 17th is already the 18th in, say, Sydney; the archive
    // is partitioned in UTC, so this must stay the 17th.
    expect(isoDay(new Date("2026-09-17T23:30:00Z"))).toBe("2026-09-17");
    expect(isoDay(new Date("2026-09-17T00:00:00Z"))).toBe("2026-09-17");
  });

  it("takes today from the clock in UTC", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-17T23:30:00Z"));
    expect(todayUTC()).toBe("2026-09-17");
  });
});

describe("addDays", () => {
  it("moves forwards and backwards", () => {
    expect(addDays("2026-09-17", 1)).toBe("2026-09-18");
    expect(addDays("2026-09-17", -3)).toBe("2026-09-14");
    expect(addDays("2026-09-17", 0)).toBe("2026-09-17");
  });

  it("crosses a month and a year boundary", () => {
    expect(addDays("2026-09-30", 1)).toBe("2026-10-01");
    expect(addDays("2026-01-01", -1)).toBe("2025-12-31");
  });
});

describe("daysBetween", () => {
  it("is inclusive at both ends and ascending", () => {
    expect(daysBetween("2026-09-15", "2026-09-17")).toEqual([
      "2026-09-15",
      "2026-09-16",
      "2026-09-17",
    ]);
    expect(daysBetween("2026-09-17", "2026-09-17")).toEqual(["2026-09-17"]);
  });

  it("gives nothing for an inverted range", () => {
    expect(daysBetween("2026-09-17", "2026-09-15")).toEqual([]);
  });

  it("caps a runaway range", () => {
    // The only thing between a hand-edited URL and thousands of fetches.
    const days = daysBetween("2000-01-01", "2026-09-17");
    expect(days).toHaveLength(MAX_RANGE_DAYS);
    expect(days[0]).toBe("2000-01-01");
  });
});

describe("minuteOfDay", () => {
  it("converts HH:MM to minutes", () => {
    expect(minuteOfDay("00:00")).toBe(0);
    expect(minuteOfDay("01:30")).toBe(90);
    expect(minuteOfDay("23:59")).toBe(1439);
  });

  it("reads junk as zero rather than NaN", () => {
    expect(minuteOfDay("")).toBe(0);
    expect(minuteOfDay("nonsense")).toBe(0);
  });
});

describe("hhmm", () => {
  it("formats a UTC time of day, zero padded", () => {
    expect(hhmm(Date.parse("2026-09-17T04:05:00Z"))).toBe("04:05");
    expect(hhmm(Date.parse("2026-09-17T23:59:00Z"))).toBe("23:59");
  });
});

describe("DAY_MS", () => {
  it("is a day in milliseconds", () => {
    expect(DAY_MS).toBe(86_400_000);
  });
});
