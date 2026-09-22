import { describe, expect, it } from "vitest";

import { severityTone } from "../utils/severity";

describe("severityTone", () => {
  it.each([
    ["info", "info"],
    ["warning", "warning"],
    ["error", "offline"],
    ["critical", "offline"],
  ])("gives %s the %s badge", (severity, tone) => {
    expect(severityTone(severity)).toBe(tone);
  });

  it.each(["debug", "", null, undefined, "constructor"])("reads an unknown severity (%s) as informational", (severity) => {
    expect(severityTone(severity)).toBe("info");
  });
});
