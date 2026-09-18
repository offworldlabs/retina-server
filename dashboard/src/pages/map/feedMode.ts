/**
 * Which fleet a map surface shows.
 *
 *   real:      the real nodes only — the server-filtered /ws/aircraft/live feed.
 *   synthetic: the simulated fleet only — the unfiltered feed with the real
 *              nodes taken off client-side (see syntheticOnly.ts).
 *   all:       everything the unfiltered feed carries (the laptop).
 *
 * The mode used to be a property of the hostname alone. It is now a property
 * of the page: /map takes the hostname's default below, /sim is always
 * `synthetic`, so one console serves both fleets at two addresses.
 */
import { hidesRealNodes, usesRealOnlyFeed } from "./utils/domains";

export type FeedMode = "real" | "synthetic" | "all";

/** The mode /map takes on this hostname. */
export function defaultFeedMode(): FeedMode {
  if (usesRealOnlyFeed) return "real";
  if (hidesRealNodes) return "synthetic";
  return "all";
}
