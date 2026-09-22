import { describe, expect, it } from "vitest";

import { detectionCount, isOnline, shortRef, statusLabel } from "../utils/nodes";

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

describe("shortRef", () => {
  it("keeps a ref's twelve random characters and drops its prefix", () => {
    expect(shortRef("nde4f2k9xq7m3b8")).toBe("4f2k9xq7m3b8");
    expect(shortRef("sim0a1b2c3d4e5f")).toBe("0a1b2c3d4e5f");
  });

  // A cut to twelve would make "synth-US-0001" read "ynth-US-0001".
  it("leaves an identifier of any other shape whole", () => {
    expect(shortRef("synth-US-0001")).toBe("synth-US-0001");
  });

  it.each([undefined, null, ""])("is empty for a missing ref (%s)", (ref) => {
    expect(shortRef(ref)).toBe("");
  });
});
