import { describe, expect, it } from "vitest";

import { challengeExpired, claimBadge, describeChallenge } from "../pages/admin/claims/claimState";
import type { AdminClaim, AdminClaimChallenge } from "../types";

const NOW = 1_790_000_000;
const HOUR = 3600;

function claim(overrides: Partial<AdminClaim> = {}): AdminClaim {
  return {
    node_id: "node-1",
    node_ref: "ret-a",
    status: "unclaimed",
    owner: null,
    email: null,
    verified: false,
    undeliverable: false,
    updated_at: null,
    owned_since: null,
    challenge: null,
    ...overrides,
  };
}

function challenge(overrides: Partial<AdminClaimChallenge> = {}): AdminClaimChallenge {
  return {
    email: "ada@example.com",
    created_at: NOW - 2 * HOUR,
    expires_at: NOW + 22 * HOUR,
    expired: false,
    ...overrides,
  };
}

const ADA = { user_id: "u1", email: "ada@example.com", name: "Ada" };

describe("claimBadge", () => {
  it("names each status the node is told, in its tone", () => {
    expect(claimBadge(claim({ status: "owned", owner: ADA, verified: true }), NOW)).toEqual({
      label: "Owned",
      tone: "online",
    });
    expect(claimBadge(claim({ status: "pending", email: "ada@example.com", challenge: challenge() }), NOW)).toEqual({
      label: "Pending",
      tone: "info",
    });
    expect(claimBadge(claim(), NOW)).toEqual({ label: "Unclaimed", tone: "private" });
    expect(claimBadge(claim({ email: "ada@example.com" }), NOW)).toEqual({ label: "Unclaimed", tone: "private" });
  });

  it("says the mail bounced before anything else, an owned node included", () => {
    const bounced = { email: "ada@example.com", undeliverable: true };
    expect(claimBadge(claim(bounced), NOW)).toEqual({ label: "Mail bounced", tone: "offline" });
    expect(claimBadge(claim({ ...bounced, status: "owned", owner: ADA }), NOW).label).toBe("Mail bounced");
    expect(
      claimBadge(claim({ ...bounced, challenge: challenge({ expires_at: NOW - HOUR, expired: true }) }), NOW).label,
    ).toBe("Mail bounced");
  });

  it("says the link expired over the unclaimed status the server derives from it", () => {
    const expired = challenge({ created_at: NOW - 3 * 86400, expires_at: NOW - 2 * 86400, expired: true });
    expect(claimBadge(claim({ email: "ada@example.com", challenge: expired }), NOW)).toEqual({
      label: "Link expired",
      tone: "warning",
    });
  });

  it("judges a pending link by the clock too, for a page left open past its expiry", () => {
    const lapsed = claim({ status: "pending", challenge: challenge({ expires_at: NOW - 1, expired: false }) });
    expect(claimBadge(lapsed, NOW).label).toBe("Link expired");
    expect(claimBadge(lapsed, NOW - 60).label).toBe("Pending");
  });

  it("calls an owned node owned whatever link is left beside it", () => {
    const leftover = challenge({ expires_at: NOW - HOUR, expired: true });
    expect(claimBadge(claim({ status: "owned", owner: ADA, challenge: leftover }), NOW).label).toBe("Owned");
  });
});

describe("challengeExpired", () => {
  it("takes the server's word, or the clock's once it has passed", () => {
    expect(challengeExpired(challenge(), NOW)).toBe(false);
    expect(challengeExpired(challenge({ expired: true }), NOW)).toBe(true);
    expect(challengeExpired(challenge({ expires_at: NOW }), NOW)).toBe(true);
  });
});

describe("describeChallenge", () => {
  it("says when a live link went out and when it runs out", () => {
    expect(describeChallenge(challenge(), NOW)).toBe("Sent 2h ago, expires in 22h");
    expect(describeChallenge(challenge({ created_at: NOW - 30, expires_at: NOW + 45 * 60 }), NOW)).toBe(
      "Sent just now, expires in 45m",
    );
    expect(describeChallenge(challenge({ created_at: NOW - 120, expires_at: NOW + 20 }), NOW)).toBe(
      "Sent 2m ago, expires in under a minute",
    );
  });

  it("says when an expired one ran out", () => {
    const expired = challenge({ created_at: NOW - 3 * 86400, expires_at: NOW - 2 * 86400, expired: true });
    expect(describeChallenge(expired, NOW)).toBe("Sent 3d ago, expired 2d ago");
  });

  it("reads a server clock running ahead as now, never as the future", () => {
    const ahead = challenge({ created_at: NOW + 90, expires_at: NOW + 30, expired: true });
    expect(describeChallenge(ahead, NOW)).toBe("Sent just now, expired just now");
  });
});
