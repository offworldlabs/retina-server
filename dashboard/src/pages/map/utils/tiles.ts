/**
 * The basemap tile sources, one copy for every map in the console. The Carto
 * ones still need withCartoKey (basemap.ts) at the call site.
 *
 * Positron is spelled two ways, both of which Carto serves: the live map's
 * `rastertiles/light_all` and the plain `light_all` the smaller maps use.
 */
export const TILES = {
  cartoVoyager: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  cartoPositron: "https://{s}.basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}{r}.png",
  cartoLight: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  cartoDark: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
  // One host: OpenStreetMap has deprecated its a/b/c subdomains.
  osm: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
} as const;
