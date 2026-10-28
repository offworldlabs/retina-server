import type { AdminClaim, AdminClaimChallenge } from "../../../types";

/** A `.badge` tone from ui.css. */
export type ClaimTone = "online" | "info" | "warning" | "offline" | "private";

export interface ClaimBadge {
  label: string;
  tone: ClaimTone;
}

const nowSeconds = () => Date.now() / 1000;

/** Whether the claim link has run out. The server's flag is as of its read,
 *  so a page left open also goes by the clock. Times are epoch seconds. */
export function challengeExpired(challenge: AdminClaimChallenge, now: number = nowSeconds()): boolean {
  return challenge.expired || challenge.expires_at <= now;
}

/**
 * What a claim's badge says, and in which tone. The two things that leave a
 * claim stuck outrank the status the node is told, since they are what an
 * administrator is looking for: a bounced address first, whatever the status,
 * then an expired link. An owner settles a claim, so a link left over beside
 * one is not called expired.
 */
export function claimBadge(claim: AdminClaim, now: number = nowSeconds()): ClaimBadge {
  if (claim.undeliverable) return { label: "Mail bounced", tone: "offline" };
  if (claim.status !== "owned" && claim.challenge && challengeExpired(claim.challenge, now)) {
    return { label: "Link expired", tone: "warning" };
  }
  switch (claim.status) {
    case "owned":
      return { label: "Owned", tone: "online" };
    case "pending":
      return { label: "Pending", tone: "info" };
    default:
      return { label: "Unclaimed", tone: "private" };
  }
}

/** How far `at` is from `now`, in the coarsest unit that is not zero and the
 *  units formatRelativeTime uses: "3h ago", or "in 3h" for a moment to come. */
function relative(at: number, now: number): string {
  const s = Math.round(at - now);
  const past = s <= 0;
  const span = Math.abs(s);
  if (span < 60) return past ? "just now" : "in under a minute";
  const amount =
    span < 3600 ? `${Math.floor(span / 60)}m` : span < 86400 ? `${Math.floor(span / 3600)}h` : `${Math.floor(span / 86400)}d`;
  return past ? `${amount} ago` : `in ${amount}`;
}

/** The claim link in a line: when it was sent, and when it runs out or ran
 *  out. "Sent 2h ago, expires in 22h"; "Sent 3d ago, expired 2d ago". A
 *  server clock ahead of this one reads as now rather than as the future. */
export function describeChallenge(challenge: AdminClaimChallenge, now: number = nowSeconds()): string {
  const sent = `Sent ${relative(Math.min(challenge.created_at, now), now)}`;
  if (challengeExpired(challenge, now)) return `${sent}, expired ${relative(Math.min(challenge.expires_at, now), now)}`;
  return `${sent}, expires ${relative(challenge.expires_at, now)}`;
}
