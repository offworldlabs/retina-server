import { LocationPrivacyBadge } from "../../../components/LocationPrivacy";
import { YoursBadge } from "../../../components/YoursBadge";
import type { NodeMark as Mark } from "./nodes";

/** A node's mark, in the badges every other page marks the owner's nodes
 *  with; nothing for a node that has none. */
export function NodeMark({ mark }: { mark: Mark | undefined }) {
  if (mark === "yours") return <YoursBadge />;
  if (mark === "private") return <LocationPrivacyBadge isPrivate />;
  return null;
}
