import { request } from "@retina/shared";

const API_BASE = "/api";

// Wrappers accept an optional AbortSignal so unmounting components can cancel
// in-flight requests instead of resolving into setState after unmount.
// Every wrapper answers null (or an empty list) for any failure, a non-2xx
// answer, the timeout, a network error or the abort alike: the callers show
// what they have and ask again later, and none of them tells those apart.

const MLAT_VERIFICATION_TTL_MS = 5000;
let mlatVerificationCache: unknown = null;
let mlatVerificationCacheTs = 0;
let mlatVerificationInflight: Promise<unknown | null> | null = null;

export async function fetchMlatVerification() {
  const now = Date.now();
  if (mlatVerificationCache && (now - mlatVerificationCacheTs) < MLAT_VERIFICATION_TTL_MS) {
    return mlatVerificationCache;
  }

  if (mlatVerificationInflight) {
    return mlatVerificationInflight;
  }

  mlatVerificationInflight = (async () => {
    try {
      const data = await request(`${API_BASE}/test/mlat-verification`);
      mlatVerificationCache = data;
      mlatVerificationCacheTs = Date.now();
      return data;
    } catch {
      return null;
    }
  })();

  try {
    return await mlatVerificationInflight;
  } finally {
    mlatVerificationInflight = null;
  }
}

export async function fetchMlatAccuracy(signal?: AbortSignal) {
  try {
    return await request(`${API_BASE}/test/mlat-accuracy`, { signal });
  } catch {
    return null;
  }
}

// Per-solve history for one MLAT map marker (mn<sha256[:10]> hex): the raw
// solves behind the marker over the last ~30 min, plus gate rejections near
// its position. Debug surface — see AircraftDetailPanel's solve history.
export async function fetchMlatHistory(hex: string, signal?: AbortSignal) {
  if (!hex) return null;
  try {
    return await request(`${API_BASE}/test/mlat-history?hex=${encodeURIComponent(hex)}`, { signal });
  } catch {
    return null;
  }
}

// Returns the current user dict, or null when not authenticated (401) or unreachable.
export async function fetchMe() {
  try {
    return await request(`${API_BASE}/auth/me`, { timeoutMs: 30_000 });
  } catch {
    return null;
  }
}

// Returns the list of nodes owned by the current user ([] when unauthenticated/unreachable).
export async function fetchMyNodes() {
  try {
    return await request(`${API_BASE}/auth/me/nodes`);
  } catch {
    return [];
  }
}
