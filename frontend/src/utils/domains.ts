/**
 * Single source of truth for hostname-based feature flags.
 *
 * The backend serves several user-facing surfaces from one app, distinguished
 * only by subdomain. Each predicate here captures one concrete decision:
 *
 *   isMapDomain:            any "map" surface, on any environment. Used to
 *                           derive the public-demo predicates below.
 *   usesRealOnlyFeed:       hits /ws/aircraft/live so the synthetic fleet never
 *                           appears, even if a node leaks through a bad filter.
 *   defaultsGroundTruthOff: ADS-B ground truth starts hidden.
 *   hidesRealNodes:         drops the real fleet out of the unfiltered feed, so
 *                           a public demo shows the synthetic nodes and nothing
 *                           else.
 *
 * usesRealOnlyFeed and defaultsGroundTruthOff are both asking "is this a
 * real-radar surface?", and the answer is a property of the environment rather
 * than of the name: production and the retina-test droplet are real-only,
 * staging is synthetic. That is why the pattern takes an optional `test-`
 * prefix and excludes `staging-`. On staging the real-only feed would be empty
 * and the ground-truth overlay is the reference you are there to look at. They
 * stay separate exports because the call sites read better naming the decision
 * than the environment, but they are deliberately one test.
 *
 * hidesRealNodes asks the opposite question and is a third state, not a wider
 * version of the first: a real-radar surface wants real and not synthetic, the
 * public demo wants synthetic and not real, the laptop wants both.
 *
 * Hostnames covered by isMapDomain:
 *   app.*                              production (real-radar)
 *   staging-app.*                      staging — the public demo, the only
 *                                      environment still running a fleet
 *   test-app.*                         the retina-test droplet (real-radar)
 *   app.localhost                      the laptop Docker stack
 *
 * These names serve the map at `/` and mount the other bundles under sub-paths,
 * so this file is loaded on the map surface alone.
 *
 * Hostname is read once at module load — we never switch domains at runtime.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

// The environment prefix is optional: production carries none.
export const isMapDomain = /^(staging-|test-)?app\./i.test(HOSTNAME);

// The real-radar surfaces. Anchored to the bare `app.` name, with the test
// droplet's `test-` environment prefix as the one permitted addition:
// `staging-app` is synthetic and must NOT match. Keeping the anchoring this
// tight is the point — were `staging-app` to match, staging's map would be
// emptied by the real-only feed. The laptop is ruled out by suffix rather than
// prefix, because it is the one environment that does not carry its name in the
// prefix: `app.localhost` has production's exact shape.
const isRealRadar = /^(test-)?app\./i.test(HOSTNAME) && !/\.localhost$/i.test(HOSTNAME);

export const usesRealOnlyFeed = isRealRadar;
export const defaultsGroundTruthOff = isRealRadar;

// The public demo surfaces: every map surface that is not real-radar. Derived
// from isMapDomain rather than matched against its own prefix list, so a
// hostname added to that regex is covered here without a second edit.
//
// Widening usesRealOnlyFeed to cover these is the change this exists to
// prevent: the real-only feed carries no synthetic fleet, so it would empty the
// very map they exist to demonstrate. They stay on the unfiltered feed and the
// real nodes come off client-side instead, decided from the server's
// is_synthetic flag (see utils/nodeKind.ts).
const isPublicDemo = isMapDomain && !isRealRadar;

// Ruled out on the laptop by the same suffix test as isRealRadar: a local
// stack has no public audience, and hiding half its fleet would only make the
// dev map disagree with the feed behind it.
export const hidesRealNodes = isPublicDemo && !/\.localhost$/i.test(HOSTNAME);
