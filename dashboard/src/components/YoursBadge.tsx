/** Marks a row or a tile as the signed-in owner's own node. The leaderboard's
 *  tag, so the mark reads the same on every page; `.badge.yours` keeps its
 *  text readable on a washed row. */
export function YoursBadge() {
  return <span className="badge plain yours">Yours</span>;
}
