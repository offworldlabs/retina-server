import { lazy, Suspense } from "react";

import ErrorBoundary from "../../../components/ErrorBoundary";
import { isAdminHost } from "../../../utils/surface";

// Its own chunk, fetched by the admin console alone: the owners' node page
// shares this module and never shows the card.
const ClaimCard = lazy(() => import("./ClaimCard"));

/** A node's claim as an administrator reads it, with the owner reassign, on
 *  the admin console's node page. Renders nothing on the owners' surface,
 *  which has no admin routes. A card that fails to load or to render costs
 *  the card alone, not the page above it. */
export function AdminClaimCard({ nodeRef }: { nodeRef: string }) {
  if (!isAdminHost(window.location.hostname)) return null;
  return (
    <ErrorBoundary title="Could not show the claim" message="The rest of this page still works. Reload it to try again.">
      <Suspense fallback={null}>
        <ClaimCard name={nodeRef} />
      </Suspense>
    </ErrorBoundary>
  );
}
