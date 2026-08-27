/**
 * Attaches the CARTO API key to basemap tile URLs, when there is one.
 *
 * The key arrives as a build-time value (VITE_CARTO_API_KEY, threaded from the
 * droplet's gitignored env through the Dockerfile's frontend stage), which
 * means it is baked into the shipped bundle and readable by anyone who opens
 * devtools. That is the supported shape for a basemap token rather than a
 * mistake: tile requests are issued by the browser, so no server sits in the
 * path that could hold a secret. It does mean this must only ever carry a key
 * scoped to fetching tiles.
 *
 * Setting it is optional, and today it changes nothing. As of 2026-08-27 the
 * public raster endpoints under basemaps.cartocdn.com serve tiles anonymously
 * and ignore the parameter outright — a deliberately bogus api_key returns a
 * byte-identical tile — so this exists so that moving to a keyed endpoint or a
 * paid plan is a value change rather than a code change. Leave the build arg
 * unset and every URL below is passed through exactly as it was written.
 */
const CARTO_API_KEY = (import.meta.env.VITE_CARTO_API_KEY ?? "").trim();

/**
 * The only host the key belongs to. The map layer switcher also offers OSM,
 * and the ternary that picks between them hands whichever it chose straight to
 * this function, so a URL that is not Carto's must come back untouched — an
 * api_key on tile.openstreetmap.org would be a stray query param at best.
 */
const CARTO_HOST = "basemaps.cartocdn.com";

export function withCartoKey(url: string): string {
  if (!CARTO_API_KEY || !url.includes(CARTO_HOST)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}api_key=${encodeURIComponent(CARTO_API_KEY)}`;
}
