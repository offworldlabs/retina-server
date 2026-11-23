import type { LeafletEventHandlerFnMap, Map as LeafletMap } from "leaflet";
import { useEvents, useMap } from "./core";

/** Binds map events while mounted, re-binding when `handlers` is a new
 *  object, and returns the map. */
export function useMapEvents(handlers: LeafletEventHandlerFnMap): LeafletMap {
  const map = useMap();
  useEvents(map, handlers);
  return map;
}
