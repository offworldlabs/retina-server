/**
 * Single source of truth for hostname-based feature flags.
 *
 * The backend serves several user-facing surfaces from one app, distinguished
 * only by subdomain. Each predicate here captures one concrete decision:
 *
 *   isMapDomain:            any "map" surface, on any environment.
 *   usesRealOnlyFeed:       this host's /map shows the real fleet, reached via
 *                           /ws/aircraft/live so the synthetic fleet never
 *                           appears, even if a node leaks through a bad filter.
 *   defaultsGroundTruthOff: ADS-B ground truth starts hidden.
 *
 * What the hostname settles is only a DEFAULT, and only /map's. Which fleet a
 * map page shows is a property of the page (see pages/map/feedMode.ts): /sim
 * asks for the synthetic fleet on every host, which is how one console serves
 * both fleets at two addresses. So these predicates belong behind
 * defaultFeedMode() and not at a render site — a call site that reads them
 * directly answers for the hostname when the page has already answered for
 * itself, and /sim would show the real fleet on test-app.
 *
 * usesRealOnlyFeed and defaultsGroundTruthOff are both asking "is this a
 * deployed environment?", and every deployed environment's /map is the real
 * network now that the synthetic fleet has a page of its own. Staging used to
 * be the exception — its map WAS the public demo, and the hostname carried
 * that — but /sim is where the demo lives on every host, so a staging-app
 * /map that hid the real nodes was showing the wrong fleet under the address
 * that names the real one. They stay separate exports because the call sites
 * read better naming the decision than the environment, but they are
 * deliberately one test.
 *
 * Hostnames covered by isMapDomain:
 *   app.*                              production
 *   staging-app.*                      staging
 *   test-app.*                         the retina-test droplet
 *   app.localhost                      the laptop Docker stack, whose /map
 *                                      keeps both fleets (see feedMode.ts)
 *
 * This file is read on every console page, admin included. That is safe
 * because each flag is a pure hostname test that cannot throw on a host it
 * does not recognise; an unfamiliar host simply matches nothing.
 *
 * Hostname is read once at module load — we never switch domains at runtime.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

// The environment prefix is optional: production carries none.
export const isMapDomain = /^(staging-|test-)?app\./i.test(HOSTNAME);

// The deployed map surfaces: every environment prefix isMapDomain accepts. The
// laptop is ruled out by suffix rather than prefix, because it is the one
// environment that does not carry its name in the prefix: `app.localhost` has
// production's exact shape. Its /map shows both fleets, because a local stack
// has no public audience and a filtered map would only disagree with the feed
// behind it.
const isRealRadar = isMapDomain && !/\.localhost$/i.test(HOSTNAME);

export const usesRealOnlyFeed = isRealRadar;
export const defaultsGroundTruthOff = isRealRadar;
