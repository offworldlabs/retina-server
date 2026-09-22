/**
 * Which fleet this host's /map shows by default: the real network on every
 * deployed environment, reached via /ws/aircraft/live so the synthetic fleet
 * never appears there even if a node leaks through a bad filter, and both
 * fleets on the laptop stack. Sibling of surface.ts, which reads the same
 * hostname to pick the console.
 *
 * Only a default, and only /map's. Which fleet a map page shows is a property
 * of the page (pages/map/feedMode.ts): /sim asks for the synthetic fleet on
 * every host. So this belongs behind defaultFeedMode() and not at a render
 * site, where it would answer for the hostname after the page had answered for
 * itself.
 *
 * The hostname is read once, at module load.
 */

const HOSTNAME = typeof window !== "undefined" ? window.location.hostname : "";

// app.* (production), staging-app.* and test-app.*. The laptop's
// app.localhost has production's shape, so it is ruled out by suffix: a local
// stack has no public audience, and a filtered map there would only disagree
// with the feed behind it.
const DEPLOYED_APP_HOST = /^(staging-|test-)?app\./i;

export const usesRealOnlyFeed =
  DEPLOYED_APP_HOST.test(HOSTNAME) && !/\.localhost$/i.test(HOSTNAME);
