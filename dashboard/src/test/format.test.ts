import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fmt, formatBytes, formatRelativeTime, formatUptime } from "../utils/format";

describe("fmt", () => {
  it("rounds to two decimals by default", () => {
    expect(fmt(1.2345)).toBe("1.23");
    expect(fmt(0)).toBe("0.00");
  });

  it("takes an explicit number of decimals", () => {
    expect(fmt(1.2345, 1)).toBe("1.2");
    expect(fmt(5, 0)).toBe("5");
    expect(fmt(0.1234, 3)).toBe("0.123");
  });

  it.each([undefined, null, NaN])("renders %s as a dash", (v) => {
    expect(fmt(v)).toBe("—");
  });
});

describe("formatUptime", () => {
  it.each([0, undefined, null])("renders %s as a dash", (v) => {
    expect(formatUptime(v)).toBe("—");
  });

  it("counts hours and minutes under a day", () => {
    expect(formatUptime(59)).toBe("0h 0m");
    expect(formatUptime(3661)).toBe("1h 1m");
    expect(formatUptime(23 * 3600 + 59 * 60)).toBe("23h 59m");
  });

  it("switches to days and hours past a full day", () => {
    expect(formatUptime(25 * 3600)).toBe("1d 1h");
    expect(formatUptime(3 * 86400 + 5 * 3600 + 30 * 60)).toBe("3d 5h");
  });

  // The threshold is strictly more than 24 hours: a node up for exactly a day
  // still reads in hours.
  it("keeps exactly 24 hours in hours", () => {
    expect(formatUptime(86400)).toBe("24h 0m");
  });
});

describe("formatRelativeTime", () => {
  const now = new Date("2026-09-15T12:00:00Z");

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const secondsAgo = (s: number) => new Date(now.getTime() - s * 1000).toISOString();

  it.each([undefined, null, ""])("renders %s as a dash", (v) => {
    expect(formatRelativeTime(v)).toBe("—");
  });

  it("calls anything under five seconds just now", () => {
    expect(formatRelativeTime(secondsAgo(0))).toBe("just now");
    expect(formatRelativeTime(secondsAgo(4))).toBe("just now");
  });

  it("counts seconds, then minutes, then hours", () => {
    expect(formatRelativeTime(secondsAgo(5))).toBe("5s ago");
    expect(formatRelativeTime(secondsAgo(59))).toBe("59s ago");
    expect(formatRelativeTime(secondsAgo(60))).toBe("1m ago");
    expect(formatRelativeTime(secondsAgo(59 * 60 + 59))).toBe("59m ago");
    expect(formatRelativeTime(secondsAgo(3600))).toBe("1h ago");
    expect(formatRelativeTime(secondsAgo(3 * 86400))).toBe("72h ago");
  });
});

describe("formatBytes", () => {
  it("keeps bytes under a kilobyte whole", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  it("gives kilobytes and megabytes one decimal", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("gives gigabytes two decimals", () => {
    expect(formatBytes(1024 ** 3)).toBe("1.00 GB");
    expect(formatBytes(2.25 * 1024 ** 3)).toBe("2.25 GB");
  });

  it("shows a dash for a value that is not a number", () => {
    expect(formatBytes(NaN)).toBe("—");
    expect(formatBytes(undefined)).toBe("—");
  });
});
