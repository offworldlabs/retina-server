import { isOnline, statusLabel } from "../utils/nodes";

type Props =
  /** The node's `status` field, judged by isOnline. */
  | { status: string | null | undefined }
  /** A verdict the server has already reached, as the leaderboard sends. */
  | { online: boolean };

/** A node's liveness as a chip in ui.css's `.badge` vocabulary, worded by
 *  statusLabel. */
export function StatusBadge(props: Props) {
  const online = "online" in props ? props.online : isOnline(props.status);
  const label = "online" in props ? (online ? "Online" : "Offline") : statusLabel(props.status);
  return <span className={`badge ${online ? "online" : "offline"}`}>{label}</span>;
}
