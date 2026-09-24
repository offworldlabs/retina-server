import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fmt,
  formatAvailability,
  formatBytes,
  formatDuration,
  formatMHz,
  formatPercent,
  formatRelativeTime,
} from "../utils/format";

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

describe("formatAvailability", () => {
  it.each([undefined, null])("renders %s as a dash", (v) => {
    expect(formatAvailability(v)).toBe("—");
  });

  it("reads a share as a percentage to one place", () => {
    expect(formatAvailability(0)).toBe("0.0%");
    expect(formatAvailability(0.9731)).toBe("97.3%");
    expect(formatAvailability(1)).toBe("100.0%");
  });

  // One minute missed in a week is 99.99%, which rounded would claim a
  // perfect week.
  it("rounds down, so only a perfect week reads 100%", () => {
    expect(formatAvailability(0.9999)).toBe("99.9%");
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

  it.each([undefined, null, "", 0])("renders %s as a dash", (v) => {
    expect(formatRelativeTime(v)).toBe("—");
  });

  // Rather than as "NaNh ago".
  it("renders a timestamp that does not parse as a dash", () => {
    expect(formatRelativeTime("not-a-date")).toBe("—");
  });

  it("calls anything under five seconds just now", () => {
    expect(formatRelativeTime(secondsAgo(0))).toBe("just now");
    expect(formatRelativeTime(secondsAgo(4))).toBe("just now");
  });

  it("counts seconds, then minutes, then hours, then days", () => {
    expect(formatRelativeTime(secondsAgo(5))).toBe("5s ago");
    expect(formatRelativeTime(secondsAgo(59))).toBe("59s ago");
    expect(formatRelativeTime(secondsAgo(60))).toBe("1m ago");
    expect(formatRelativeTime(secondsAgo(59 * 60 + 59))).toBe("59m ago");
    expect(formatRelativeTime(secondsAgo(3600))).toBe("1h ago");
    expect(formatRelativeTime(secondsAgo(86400 - 1))).toBe("23h ago");
    expect(formatRelativeTime(secondsAgo(86400))).toBe("1d ago");
    expect(formatRelativeTime(secondsAgo(3 * 86400))).toBe("3d ago");
  });

  it("reads a number as epoch seconds", () => {
    expect(formatRelativeTime(now.getTime() / 1000 - 90)).toBe("1m ago");
  });
});

describe("formatDuration", () => {
  it.each([undefined, null, NaN])("renders %s as a dash", (v) => {
    expect(formatDuration(v)).toBe("—");
  });

  it("counts whole seconds, then whole minutes, then tenths of an hour", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(120)).toBe("2m");
    expect(formatDuration(5400)).toBe("1.5h");
  });

  // Rounding up to the hour must not print "60m".
  it("moves to hours rather than showing sixty minutes", () => {
    expect(formatDuration(3590)).toBe("1.0h");
  });
});

describe("formatPercent", () => {
  it("rounds to one decimal by default", () => {
    expect(formatPercent(31.24)).toBe("31.2%");
    expect(formatPercent(0)).toBe("0.0%");
  });

  it("takes an explicit number of decimals", () => {
    expect(formatPercent(99.974, 2)).toBe("99.97%");
  });

  it.each([undefined, null, NaN])("renders %s as a dash, with no percent sign", (v) => {
    expect(formatPercent(v)).toBe("—");
  });
});

describe("formatMHz", () => {
  it("shows megahertz to the kilohertz", () => {
    expect(formatMHz(174_928_000)).toBe("174.928 MHz");
    expect(formatMHz(195_000_000)).toBe("195.000 MHz");
    expect(formatMHz(98_100_000)).toBe("98.100 MHz");
  });

  it.each([undefined, null, NaN, 0])("renders %s as a dash", (v) => {
    expect(formatMHz(v)).toBe("—");
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
