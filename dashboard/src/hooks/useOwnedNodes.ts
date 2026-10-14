import { useMemo } from "react";
import { UnauthorizedError } from "@retina/shared";

import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";
import type { OwnedNode } from "../types";
import { isAdminHost } from "../utils/surface";
import { usePolling } from "./usePolling";

/**
 * The signed-in owner's nodes, private ones included: the one list every page
 * scopes "my nodes" by, asked for the same way everywhere.
 */

/** "signed-out" also once the list's own request answers 401: the session
 *  ended elsewhere, whatever the identity on screen still says. */
export type OwnedNodesStatus = "signed-out" | "loading" | "ready" | "error";

export interface OwnedNodes {
  status: OwnedNodesStatus;
  /** Empty unless ready. */
  nodes: OwnedNode[];
  /** The listed nodes' node_refs, the key every public listing uses, so a
   *  page can mark the owner's rows. A node with no ref is left out. */
  refs: ReadonlySet<string>;
  /** The first request's failure while "error"; a later refresh's while
   *  "ready", with the last list still in `nodes`. */
  error: Error | null;
  refresh: () => void;
}

/** An answer carries whose list it is, so the previous owner's list is never
 *  shown as the next one's while the new answer is on its way. `ended` is a
 *  401 for that owner: no list, and no session to ask again with. It carries
 *  the session it was asked under as well, since that 401 says nothing of a
 *  session adopted after it, even one of the same owner's. */
interface Answer {
  owner: string;
  session: number;
  nodes: OwnedNode[];
  ended?: true;
}

const NO_NODES: OwnedNode[] = [];

// Nothing to ask for: settles at once, so no request leaves the page.
const nothing = () => Promise.resolve(null);

function refsOf(nodes: OwnedNode[]): ReadonlySet<string> {
  return new Set(nodes.map((n) => n.node_ref).filter((ref): ref is string => typeof ref === "string" && ref !== ""));
}

/** `every` is the refresh period in milliseconds, 0 to fetch once. */
export function useOwnedNodes(every = 60_000): OwnedNodes {
  const { user, loading, session } = useAuth();
  // The admin console signs in through Access and has no owner views, so it
  // never asks, whoever its session belongs to.
  const admin = isAdminHost(window.location.hostname);
  const owner: string | null = !admin && !loading && user ? String(user.id) : null;

  const polled = usePolling<Answer | null>(
    owner === null
      ? nothing
      : () =>
          api.myNodes().then(
            (answer): Answer => ({ owner, session, nodes: Array.isArray(answer) ? answer : NO_NODES }),
            // An answer rather than a failure, so it replaces the last list
            // instead of being shown beside it as a refresh that failed. On an
            // open page the client has told the auth context as well, and the
            // identity it drops takes the asking with it.
            (e: unknown): Answer => {
              if (e instanceof UnauthorizedError) return { owner, session, nodes: NO_NODES, ended: true };
              throw e;
            },
          ),
    owner === null ? 0 : every,
    // Keyed on the user and the session, so signing in, out, as someone else or
    // in again asks again.
    owner === null ? "" : `user:${owner}@${session}`,
  );

  const mine = owner !== null && polled.data?.owner === owner && polled.data.session === session ? polled.data : null;
  let status: OwnedNodesStatus;
  if (owner === null) status = admin || !loading ? "signed-out" : "loading";
  else if (mine?.ended) status = "signed-out";
  else if (mine) status = "ready";
  // Not while pending: an error left from the previous user, or from the
  // attempt a retry is replacing, is not this request's answer.
  else if (polled.error && !polled.pending) status = "error";
  else status = "loading";

  const nodes = mine ? mine.nodes : NO_NODES;
  const refs = useMemo(() => refsOf(nodes), [nodes]);
  const error = status === "ready" || status === "error" ? polled.error : null;
  const { refresh } = polled;

  return useMemo(() => ({ status, nodes, refs, error, refresh }), [status, nodes, refs, error, refresh]);
}
