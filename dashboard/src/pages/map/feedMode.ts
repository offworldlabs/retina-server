/**
 * Which fleet a map surface shows.
 *
 *   real:      the real nodes only — the server-filtered /ws/aircraft/live feed.
 *   synthetic: the simulated fleet only — the unfiltered feed with the real
 *              nodes taken off client-side (see syntheticOnly.ts).
 *   all:       everything the unfiltered feed carries (the laptop).
 *
 * The mode is a property of the page: the app's /map takes the hostname's
 * default below, and the admin console's /sim is always `synthetic`. No
 * hostname defaults to `synthetic`; /sim is the only way to ask for it.
 */
import { usesRealOnlyFeed } from "../../utils/domains";

export type FeedMode = "real" | "synthetic" | "all";

/** The mode /map takes on this hostname. */
export function defaultFeedMode(): FeedMode {
  return usesRealOnlyFeed ? "real" : "all";
}
