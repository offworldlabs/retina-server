import L, { type Map as LeafletMap, type MapOptions } from "leaflet";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type ReactNode,
} from "react";
import { MapContext, leafletOptions } from "./core";

export interface MapContainerProps extends MapOptions {
  children?: ReactNode;
  className?: string;
  id?: string;
  style?: CSSProperties;
}

/**
 * The map a MapContainer holds, or null while it holds none. Render reads it
 * from here rather than from React state, so it always gets the map that is
 * live at that moment. A state update could lag behind: in development
 * StrictMode removes the first map and builds a second, and that second
 * update can land at a lower priority than the first, so React would render
 * the children with the removed map before it applied the live one.
 */
function createMapStore() {
  let map: LeafletMap | null = null;
  const listeners = new Set<() => void>();
  const set = (next: LeafletMap | null) => {
    map = next;
    listeners.forEach((listener) => listener());
  };
  return {
    get: () => map,
    /** Builds the map, unless one is held. */
    ensure(build: () => LeafletMap) {
      if (!map) set(build());
    },
    /** Removes the held map, if any; the store holds none from then on. */
    remove() {
      const held = map;
      if (!held) return;
      set(null);
      held.remove();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * A div with a Leaflet map in it. The map options, center and zoom included,
 * are read once at mount; move the map afterwards through useMap(). The
 * children render once the map exists, and never with a map that has been
 * removed.
 *
 * The map is built while the container is laid out and shown, and removed in
 * a passive effect's cleanup, so only on unmount. A Suspense boundary that
 * hides the map runs only layout cleanups: the map stays, the effects still
 * running in the hidden children keep a live map, and the children come back
 * to the same map when it shows again. The passive effect builds the map too
 * when it finds none while shown: when React runs both effects again as an
 * update, its cleanup removes the old map after the layout effect has run.
 */
export const MapContainer = forwardRef<LeafletMap, MapContainerProps>(function MapContainer(props, ref) {
  const { children, className, id, style } = props;
  const [options] = useState(() => leafletOptions(props, "className", "id", "style"));
  const [store] = useState(createMapStore);
  const container = useRef<HTMLDivElement>(null);
  const shown = useRef(false);
  const map = useSyncExternalStore(store.subscribe, store.get);
  useLayoutEffect(() => {
    shown.current = true;
    store.ensure(() => L.map(container.current, options));
    return () => {
      shown.current = false;
    };
  }, [options, store]);
  useEffect(() => {
    if (shown.current) store.ensure(() => L.map(container.current, options));
    return () => store.remove();
  }, [options, store]);
  useImperativeHandle(ref, () => map, [map]);
  const context = useMemo(() => (map ? { map } : null), [map]);
  return (
    <div ref={container} id={id} className={className} style={style}>
      {context && <MapContext.Provider value={context}>{children}</MapContext.Provider>}
    </div>
  );
});
