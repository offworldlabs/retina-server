// @vitest-environment node
import { describe, expect, it } from "vitest";
import { multinodeHexFromKey, sha256Hex } from "./hex";

/**
 * The reference: Node's own SHA-256, through its WebCrypto (node:crypto's
 * webcrypto, the global crypto in the node environment). Typed by the DOM lib,
 * as the dashboard carries no Node type definitions.
 */
async function nodeSha256(text: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

describe("sha256Hex", () => {
  it("matches the FIPS 180-2 one-block vector", async () => {
    expect(await nodeSha256("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("matches the FIPS 180-2 two-block vector", () => {
    expect(sha256Hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")).toBe(
      "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
    );
  });

  it("matches Node's SHA-256 on every length around the padding boundaries", async () => {
    // 55 bytes is the longest message whose padding fits its own block, 56 the
    // shortest that spills into a second one; likewise 119/120 for two blocks.
    const lengths = [0, 1, 54, 55, 56, 57, 63, 64, 65, 118, 119, 120, 121, 127, 128, 129, 1000];
    for (const n of lengths) {
      const text = Array.from({ length: n }, (_, i) => String.fromCharCode(33 + ((i * 7) % 94))).join("");
      expect(sha256Hex(text), `length ${n}`).toBe(await nodeSha256(text));
    }
  });

  it("hashes non-ASCII text as UTF-8 across several blocks", async () => {
    const text = "Grüße aus Zürich, 東京から, ✈ over Greenville, 𝔸𝕣𝕔𝕙𝕚𝕧𝕖 — mn-dark-1760817600000-34.850--82.390";
    expect(new TextEncoder().encode(text).length).toBeGreaterThan(64);
    expect(sha256Hex(text)).toBe(await nodeSha256(text));
  });
});

describe("multinodeHexFromKey", () => {
  // Computed by backend/services/id_utils.py multinode_hex_from_key.
  it.each([
    ["mn-adsb-a1b2c3", "mnf2707d1d71"],
    ["mn-dark-1760817600000-34.850--82.390", "mn3fe22f694d"],
    ["mn-dark-3f9a", "mn352a068995"],
    ["", "mne3b0c44298"],
  ])("derives the backend's hex for %j", (key, hex) => {
    expect(multinodeHexFromKey(key)).toBe(hex);
  });
});
