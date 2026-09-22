import { describe, it, expect, vi, afterEach } from "vitest";
import { writeClipboard } from "../utils/clipboard";

function stubClipboard(writeText: ((text: string) => Promise<void>) | null) {
  Object.defineProperty(navigator, "clipboard", {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  });
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
  Reflect.deleteProperty(document, "execCommand");
});

describe("writeClipboard", () => {
  it("writes through the async API when there is one", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    expect(await writeClipboard("abc123")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("abc123");
  });

  it("reports a refusal rather than throwing", async () => {
    stubClipboard(vi.fn().mockRejectedValue(new Error("denied")));
    expect(await writeClipboard("abc123")).toBe(false);
  });

  it("copies from a hidden textarea where the async API is absent", async () => {
    stubClipboard(null);
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });
    expect(await writeClipboard("abc123")).toBe(true);
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("counts the fallback's false as a failure", async () => {
    stubClipboard(null);
    Object.defineProperty(document, "execCommand", {
      value: vi.fn().mockReturnValue(false),
      configurable: true,
    });
    expect(await writeClipboard("abc123")).toBe(false);
  });
});
