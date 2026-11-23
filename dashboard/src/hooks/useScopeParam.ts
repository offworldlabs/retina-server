import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

/** Whose data a page shows: the signed-in owner's nodes, or every node. */
export type Scope = "mine" | "network";

const PARAM = "scope";

/**
 * The scope a page shows, kept in its query string as `?scope=network` so a
 * link or a reload opens the same view. "mine" is the default and is never
 * written. Where there is no owner view to offer (signed out, or the admin
 * console), pass `available` false and the page reads "network" whatever the
 * link says.
 */
export function useScopeParam(available: boolean): [Scope, (next: Scope) => void] {
  const [params, setParams] = useSearchParams();
  const scope: Scope = !available || params.get(PARAM) === "network" ? "network" : "mine";

  const setScope = useCallback(
    (next: Scope) =>
      setParams(
        (current) => {
          const updated = new URLSearchParams(current);
          if (next === "network") updated.set(PARAM, "network");
          else updated.delete(PARAM);
          return updated;
        },
        // A different view of the same page, not a new place: Back leaves the
        // page rather than flipping the switch.
        { replace: true },
      ),
    [setParams],
  );

  return [scope, setScope];
}
