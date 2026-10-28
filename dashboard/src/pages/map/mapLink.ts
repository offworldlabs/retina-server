import { clampSpan, formatHashT } from "./useUrlHashState";

export interface MapLinkView {
  lat: number;
  lon: number;
  z: number;
  t: number;
  span?: number;
  hex?: string | null;
}

/**
 * The deep link the archive map opens on. `t` floors to the second as the hash
 * does, so a caller that needs an instant included rounds it up first.
 */
export function mapLink(view: MapLinkView): string {
  const { lat, lon, z, t, span, hex } = view;
  for (const [name, value] of [["lat", lat], ["lon", lon], ["z", z], ["t", t]] as const) {
    if (!Number.isFinite(value)) throw new RangeError(`mapLink: ${name} is not finite`);
  }
  const parts = [`lat=${lat.toFixed(4)}`, `lon=${lon.toFixed(4)}`, `z=${z}`];
  if (hex) parts.push(`hex=${encodeURIComponent(hex)}`);
  parts.push(`t=${formatHashT(t)}`);
  const s = clampSpan(span);
  if (s !== undefined) parts.push(`span=${s}`);
  return `/map#${parts.join("&")}`;
}
