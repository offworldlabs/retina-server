/**
 * Whether a node is part of a synthetic fleet.
 *
 * The server ships `is_synthetic` on every /api/radar/nodes entry, and that is
 * the answer. The prefix fallback covers only payloads predating the flag:
 * once identities are published as node_ref, no prefix survives to match.
 */
const SYNTHETIC_PREFIXES = ["synth-", "e2e-", "test-", "realnode-"];

export function isSyntheticNode(info: { is_synthetic?: boolean }, id: string): boolean {
  if (typeof info?.is_synthetic === "boolean") return info.is_synthetic;
  return SYNTHETIC_PREFIXES.some((p) => id.startsWith(p));
}
