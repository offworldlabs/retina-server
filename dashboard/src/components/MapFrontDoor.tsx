import { Navigate, useLocation } from "react-router-dom";

/**
 * The user surface's index. The map is the front page, at its own address, and
 * the old map's links carry their view in the hash, so that travels with it.
 */
export default function MapFrontDoor() {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: "/map", search, hash }} replace />;
}
