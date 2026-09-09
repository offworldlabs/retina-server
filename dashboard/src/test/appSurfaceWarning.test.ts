import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from "vitest";
import { MODE_IGNORED_WARNING } from "../utils/surface";

// App decides the surface at module scope, so proving it acts on
// resolveSurface's answer rather than merely computing it means loading it. One
// case only: warnIfModeIgnored's own branching is covered in surface.test.ts.
//
// The assertion has to go through console. vi.resetModules() means the App
// imported below gets a fresh copy of ../utils/surface, so spying on that
// module here would attach to a different instance and never fire. Comparing
// the message by value is fine: both copies produce the same string.
describe("App wires the surface warning up", () => {
  let restoreLocation: PropertyDescriptor | undefined;
  let warn: MockInstance<typeof console.warn>;

  beforeEach(() => {
    vi.resetModules();
    restoreLocation = Object.getOwnPropertyDescriptor(window, "location");
    if (!restoreLocation) {
      throw new Error(
        "window.location is not an own property here, so it cannot be restored after this test. Stubbing it anyway would leak into whatever runs next."
      );
    }
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        hostname: "dash.retina.fm",
        search: "?mode=admin",
        href: "https://dash.retina.fm/?mode=admin",
      },
    });
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    // Guarded so that when the throw above fires, its message is what surfaces
    // rather than a TypeError from restoring a descriptor never captured.
    if (restoreLocation) Object.defineProperty(window, "location", restoreLocation);
    vi.restoreAllMocks();
  });

  it("warns on a vhost that does not honour ?mode=admin", async () => {
    await import("../App");
    // Filtered rather than counted: anything in App's import graph may warn.
    const ours = warn.mock.calls.filter((args) => args[0] === MODE_IGNORED_WARNING);
    expect(ours).toHaveLength(1);
  });
});
