import { api } from "../api/client";
import { useFetch } from "./usePolling";

/** `{node_ref: node_id}` for the fleet, or `null` until the answer is in.
 *
 *  The admin pages are built on the public feeds, which are keyed on node_ref
 *  and carry no node_id at all, so this is what puts the private identifier in
 *  front of an admin: the node's own site is named after it, and the contact
 *  and owner routes are keyed on it.
 *
 *  Fetched once per mount and soft-failing to an empty map, so a page that
 *  lists the fleet is not lost with the ids it would have carried. A node that
 *  registers while the page is open therefore shows no id until it is
 *  reloaded; polling for that would cost a request per tick on a page that
 *  already polls, to catch an event that happens a few times a month.
 *
 *  The null is what keeps "not asked yet" apart from "asked, and this ref has
 *  no id": the same absence, wanting different words.
 */
export function useNodeIds(): Record<string, string> | null {
  return useFetch(() =>
    api
      .adminNodeRefs()
      .then((m) => m || {})
      .catch((e) => {
        // Logged before the fallback: an empty map is also what a fleet with no
        // registered node looks like, and the two should not read the same.
        console.error("node ids unavailable", e);
        return {};
      }),
  ).data;
}
