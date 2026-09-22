import { describe, expect, it } from "vitest";

import { detectionCount, isOnline, statusLabel } from "../utils/nodes";

describe("isOnline", () => {
  // A heartbeat may report a status of its own; only the server's two words
  // for "no connection" take a node offline.
  it.each(["active", "degraded"])("counts %s as online", (status) => {
    expect(isOnline(status)).toBe(true);
  });

  it.each(["disconnected", "never_connected"])("counts %s as offline", (status) => {
    expect(isOnline(status)).toBe(false);
  });

  it.each([null, undefined, ""])("counts a missing status (%s) as offline", (status) => {
    expect(isOnline(status)).toBe(false);
  });
});

describe("statusLabel", () => {
  it.each([
    ["active", "Online"],
    ["disconnected", "Offline"],
    [null, "Offline"],
    ["never_connected", "Never connected"],
  ])("words %s as %s", (status, label) => {
    expect(statusLabel(status)).toBe(label);
  });
});

describe("detectionCount", () => {
  it("reads the frame metrics first", () => {
    expect(detectionCount({ metrics: { total_detections: 7 }, detection_area: { n_detections: 3 } })).toBe(7);
  });

  it("falls back to the detection area when the metrics count nothing", () => {
    expect(detectionCount({ metrics: { total_detections: 0 }, detection_area: { n_detections: 42 } })).toBe(42);
  });

  it("falls back to the detection area when there are no metrics", () => {
    expect(detectionCount({ detection_area: { n_detections: 42 } })).toBe(42);
  });

  it.each([undefined, null, {}])("is zero for a summary with neither counter (%s)", (summary) => {
    expect(detectionCount(summary)).toBe(0);
  });
});
