import { Navigate, useLocation } from "react-router-dom";

/**
 * The physics page's old address.
 *
 * It used to sit at `/physics`, beside the map, because the simulator itself
 * had no address of its own — it was a hostname. Now that the simulator is a
 * page at `/sim`, the thing that tunes it belongs under it, and `/physics` is
 * a link people already have: bookmarked, and pasted into the notes of every
 * fleet-tuning session on the test droplet.
 *
 * Search and hash travel with the hop, as they do from the index
 * (MapFrontDoor). `replace` so the browser's Back button returns to wherever
 * the caller came from rather than to this address, which would forward them
 * here again.
 */
export default function PhysicsRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: "/sim/physics", search, hash }} replace />;
}
