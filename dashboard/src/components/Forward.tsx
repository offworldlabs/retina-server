import { Navigate, useLocation } from "react-router-dom";

/**
 * Forwards an address to `to`, carrying its query and hash: the old map's links
 * keep their view in the hash. `replace`, so Back returns to wherever the
 * visitor came from rather than to an address that would forward them again.
 */
export default function Forward({ to }: { to: string }) {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: to, search, hash }} replace />;
}
