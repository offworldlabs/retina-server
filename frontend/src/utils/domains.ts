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
 * staging is synthetic. That is why `map.` and `app.` sit together in one
 * pattern and take the same optional `test-` prefix, while `staging-` is
 * excluded from it. The test droplet runs a synthetic fleet alongside its real
 * nodes, and that fleet has its own surface (`test-testmap`), so `test-map` and
 * `test-app` can be the real-only view the production names are. Every other
 * map surface (testmap, staging, the laptop) is fed by the synthetic fleet,
 * where the real-only feed would be empty and the ground-truth overlay is the
 * reference you are there to look at. They stay separate exports because the
 * call sites read better naming the decision than the environment, but they are
 * deliberately one test.
 *
 * hidesRealNodes asks the opposite question and is a third state, not a wider
 * version of the first: a real-radar surface wants real and not synthetic, the
 * public demo wants synthetic and not real, the laptop wants both.
 *
 * Hostnames covered by isMapDomain:
 *   app.*                              production, consolidated (real-radar)
 *   staging-app.*                      staging, consolidated
 *   test-app.*                         the retina-test droplet, consolidated
 *                                      (real-radar)
 *   map.*                              production (real-radar)
 *   testmap.*                          the public demo — served by STAGING, not
 *                                      production, which runs no fleet
 *   staging-map.*                      staging, same data as testmap
 *   test-map.*                         the retina-test droplet (real-radar)
 *   test-testmap.*                     the retina-test droplet's synthetic fleet
 *   map.localhost  app.localhost       the laptop Docker stack
 *   testmap.localhost
 *
 * The `app` names serve the map at `/` and mount the other bundles under
 * sub-paths, so this file is loaded there on the map surface alone.
 *
 * The regexes are unchanged by testmap moving droplet: they key off the
 * hostname, which is the same string wherever it is served from.
 *
 * Hostname is read once at module load — we never switch domains at runtime.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

// The environment prefix is optional and the `test` in `testmap` is separate
// from the `test-` in `test-map.retina.fm`: the first names a synthetic surface
// within an environment, the second names the environment itself.
export const isMapDomain = /^((staging-|test-)?((test)?map|app))\./i.test(HOSTNAME);

// The real-radar surfaces. Anchored to the bare `map.` and `app.` names, with
// the test droplet's `test-` environment prefix as the one permitted addition:
// `staging-map` and `staging-app` are synthetic and must NOT match, and neither
// may anything with `testmap` in it, `test-testmap` included. Keeping the
// anchoring this tight is the point — were `staging-app` to match, staging's
// map would be emptied by the real-only feed. The laptop is ruled out by suffix
// rather than prefix, because it is the one environment that does not carry
// its name in the prefix: `map.localhost` has production's exact shape.
const isRealRadar = /^(test-)?(map|app)\./i.test(HOSTNAME) && !/\.localhost$/i.test(HOSTNAME);

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
