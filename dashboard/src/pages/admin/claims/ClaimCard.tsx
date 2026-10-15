import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import { FetchNotice, Notice, nothingLoaded } from "../../../components/Notice";
import { RelativeTime } from "../../../components/RelativeTime";
import { useFetch } from "../../../hooks/usePolling";
import type { AdminClaim, AdminClaimChallenge } from "../../../types";
import { DASH } from "../../../utils/format";
import { challengeExpired, claimBadge, describeChallenge } from "./claimState";
import { OwnerReassignDialog, type OwnerChange, type OwnerDialogMode } from "./OwnerReassignDialog";
import "./claims.css";

/** What the card found: the node's claim, or no registered node behind the
 *  page's name, which has no claim to show, since claims come with
 *  registration. */
type ClaimRead = { kind: "claim"; claim: AdminClaim } | { kind: "unregistered" };

/** The node behind a public name in the admin node map, read through its own
 *  keys only, so a name such as "constructor" finds nothing. */
function nodeIdFor(refs: unknown, name: string): string | null {
  if (!refs || typeof refs !== "object" || !Object.prototype.hasOwnProperty.call(refs, name)) return null;
  const id = (refs as Record<string, unknown>)[name];
  return typeof id === "string" && id ? id : null;
}

/** The page is named by the node's ref, or a synthetic node's own id, and the
 *  claim route takes the node_id. The admin node map joins the two for every
 *  registered node, connected or not, which is every node that can have a
 *  claim. A failed read of either is an error, not an absence. */
async function readClaim(name: string): Promise<ClaimRead> {
  const nodeId = nodeIdFor(await api.adminNodeRefs(), name);
  if (!nodeId) return { kind: "unregistered" };
  try {
    return { kind: "claim", claim: await api.adminNodeClaim(nodeId) };
  } catch (e) {
    // Connected, but never registered: a synthetic or mirrored node.
    if (e instanceof HttpError && e.status === 404) return { kind: "unregistered" };
    throw e;
  }
}

function Owner({ owner }: { owner: AdminClaim["owner"] }) {
  if (!owner) return <>Nobody</>;
  if (owner.email === null) {
    // An owner id with no account row, such as an administrator's own identity.
    return (
      <>
        Not an account
        <span className="admin-claim-sub mono">{owner.user_id}</span>
      </>
    );
  }
  return (
    <Link to={`/users/${encodeURIComponent(owner.user_id)}`} className="admin-claim-link">
      {owner.email}
    </Link>
  );
}

function ClaimLink({ challenge }: { challenge: AdminClaimChallenge | null }) {
  if (!challenge) return <>None</>;
  const expired = challengeExpired(challenge);
  return (
    <>
      {challenge.email}
      <span className={expired ? "admin-claim-sub admin-claim-expired" : "admin-claim-sub"}>
        {describeChallenge(challenge)}
      </span>
    </>
  );
}

function Facts({ claim }: { claim: AdminClaim }) {
  return (
    <table className="kv-table admin-claim-facts">
      <tbody>
        <tr>
          <td>Owner</td>
          <td>
            <Owner owner={claim.owner} />
          </td>
        </tr>
        <tr>
          <td>Claim address</td>
          <td>{claim.email ?? DASH}</td>
        </tr>
        <tr>
          <td>Verified</td>
          <td>{claim.email === null ? DASH : claim.verified ? "Yes" : "No"}</td>
        </tr>
        <tr>
          <td>Owned since</td>
          <td>
            <RelativeTime at={claim.owned_since} />
          </td>
        </tr>
        <tr>
          <td>Last updated</td>
          <td>
            <RelativeTime at={claim.updated_at} />
          </td>
        </tr>
        <tr>
          <td>Claim link</td>
          <td>
            <ClaimLink challenge={claim.challenge} />
          </td>
        </tr>
        <tr>
          <td>Node id</td>
          <td className="mono">{claim.node_id}</td>
        </tr>
      </tbody>
    </table>
  );
}

/** An administrator's reading of a node's claim: who owns it, the address on
 *  file and whether it was proved or bounced, and the claim link out, which is
 *  what a stuck claim is diagnosed from. The owner can be reassigned or
 *  removed from here. `name` is the node page's route parameter. */
export default function ClaimCard({ name }: { name: string }) {
  const polled = useFetch(() => readClaim(name), name);
  const { data, loading, refresh } = polled;
  // The claim as it stood when the dialog opened, so a reload under it does
  // not change what the dialog is about.
  const [dialog, setDialog] = useState<{ mode: OwnerDialogMode; claim: AdminClaim } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const headingId = useId();

  const root = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const reassign = useRef<HTMLButtonElement>(null);
  const unassign = useRef<HTMLButtonElement>(null);
  const recheck = useRef<HTMLButtonElement>(null);
  const refocus = useRef<OwnerDialogMode | null>(null);

  // Once the dialog has closed, focus goes back to the button that carries on
  // from it, unless the user has moved on elsewhere. Unassigning takes its own
  // button away, and Safari never focused the button clicked, so the dialog
  // has nothing reliable to give focus back to. A node found unregistered
  // under the dialog has neither button, so focus goes to Check again, and
  // failing that to the card's heading.
  useEffect(() => {
    const target = refocus.current;
    if (!target) return;
    refocus.current = null;
    const current = document.activeElement;
    if (current && current !== document.body && !root.current?.contains(current)) return;
    const button = target === "unassign" && unassign.current ? unassign.current : reassign.current;
    (button ?? recheck.current ?? heading.current)?.focus();
  });

  function close() {
    refocus.current = dialog?.mode ?? null;
    setDialog(null);
  }

  function changed(change: OwnerChange) {
    refresh();
    if (change.kind === "gone") return;
    setStatus(change.kind === "reassigned" ? `Owner changed to ${change.email}.` : "Owner removed.");
    refocus.current = "reassign";
    setDialog(null);
  }

  const claim = data?.kind === "claim" ? data.claim : null;
  const badge = claim ? claimBadge(claim) : null;

  let body;
  if (loading) {
    body = <p className="muted">Loading…</p>;
  } else if (nothingLoaded(polled)) {
    body = <FetchNotice polled={polled} what="the claim" />;
  } else if (!claim) {
    body = (
      <>
        <FetchNotice polled={polled} what="the claim" />
        <p>This node has not registered with the server, so it has no claim.</p>
        <div className="btn-row">
          <button ref={recheck} type="button" className="btn btn-secondary btn-sm" onClick={refresh}>
            Check again
          </button>
        </div>
      </>
    );
  } else {
    body = (
      <>
        <FetchNotice polled={polled} what="the claim" />
        {claim.undeliverable && (
          <Notice>
            Mail to {claim.email ?? "the claim address"} bounced. The node is told so, and asks its owner for
            another address.
          </Notice>
        )}
        <Facts claim={claim} />
        <div className="admin-claim-actions">
          <div className="btn-row">
            <button
              ref={reassign}
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                setStatus(null);
                setDialog({ mode: "reassign", claim });
              }}
            >
              {claim.owner ? "Reassign owner" : "Assign owner"}
            </button>
            {claim.owner && (
              <button
                ref={unassign}
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => {
                  setStatus(null);
                  setDialog({ mode: "unassign", claim });
                }}
              >
                Unassign
              </button>
            )}
          </div>
          <p className="admin-claim-status" role="status">
            {status}
          </p>
        </div>
      </>
    );
  }

  return (
    <section ref={root} className="card admin-claim" aria-labelledby={headingId}>
      <div className="card-header">
        <h3 ref={heading} id={headingId} className="admin-claim-heading" tabIndex={-1}>
          Claim
        </h3>
        {badge && <span className={`badge ${badge.tone}`}>{badge.label}</span>}
      </div>
      <div className="card-body card-stack">
        {body}
      </div>
      {dialog && (
        <OwnerReassignDialog
          mode={dialog.mode}
          nodeId={dialog.claim.node_id}
          nodeName={name}
          owner={dialog.claim.owner}
          onClose={close}
          onChanged={changed}
        />
      )}
    </section>
  );
}
