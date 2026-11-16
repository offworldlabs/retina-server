import {
  createContext,
  useContext,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ForwardedRef,
  type ReactNode,
} from "react";
import { createPortal, flushSync } from "react-dom";
import type {
  DivOverlay,
  Evented,
  Layer,
  LeafletEventHandlerFnMap,
  Map as LeafletMap,
  Path,
  PathOptions,
} from "leaflet";

interface MapContextValue {
  map: LeafletMap;
  /** The nearest enclosing layer: what a Popup or Tooltip binds to. */
  layer?: Layer;
}

export const MapContext = createContext<MapContextValue | null>(null);

function useMapContext(): MapContextValue {
  const value = useContext(MapContext);
  if (!value) throw new Error("Map components and hooks must be rendered inside a MapContainer");
  return value;
}

/** The Leaflet map of the enclosing MapContainer. */
export function useMap(): LeafletMap {
  return useMapContext().map;
}

export interface LayerProps {
  children?: ReactNode;
  eventHandlers?: LeafletEventHandlerFnMap;
}

export interface PathProps extends LayerProps, PathOptions {
  pathOptions?: PathOptions;
}

const NOT_OPTIONS = ["children", "eventHandlers", "pathOptions"];

/** Leaflet's Path style options: all that setStyle restyles. */
const STYLE_KEYS = [
  "stroke", "color", "weight", "opacity", "lineCap", "lineJoin", "dashArray",
  "dashOffset", "fill", "fillColor", "fillOpacity", "fillRule",
] as const;

/** The style options pathOptions holds, as given (an undefined dashArray
 *  clears the dash). pathOptions carries style only: any other key in it,
 *  className, pane or interactive among them, is ignored. */
function styleOf(pathOptions: PathOptions | undefined): Record<string, unknown> {
  const style: Record<string, unknown> = {};
  for (const key of STYLE_KEYS) if (pathOptions && key in pathOptions) style[key] = pathOptions[key];
  return style;
}

/** The props Leaflet takes as constructor options, with the style options of
 *  pathOptions laid over them. Undefined values are dropped: Leaflet would read
 *  a present-but-undefined key as a value and lose its default (a Marker's icon,
 *  a layer's pane). */
export function leafletOptions(props: LayerProps & { pathOptions?: PathOptions }, ...skip: string[]) {
  const options: Record<string, any> = {};
  for (const [key, value] of Object.entries(props)) {
    if (value !== undefined && !NOT_OPTIONS.includes(key) && !skip.includes(key)) options[key] = value;
  }
  return { ...options, ...styleOf(props.pathOptions) };
}

/** Coordinates compare by value, so a fresh [lat, lng] literal is not a move. */
export function sameCoords(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => sameCoords(item, b[i]));
  }
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  const p = a as Record<string, number>;
  const q = b as Record<string, number>;
  return p.lat === q.lat && (p.lng ?? p.lon) === (q.lng ?? q.lon) && p.alt === q.alt;
}

/** Coordinates as plain numbers, so an array the caller mutates in place and
 *  passes again still compares as a move against the copy. */
function copyCoords(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(copyCoords);
  if (!value || typeof value !== "object") return value;
  const p = value as Record<string, number>;
  return { lat: p.lat, lng: p.lng ?? p.lon, alt: p.alt };
}

const COORDINATE_PROPS = ["position", "center", "positions"];

/** One setStyle for what changed: the style options of pathOptions when it is
 *  a new object, and any top-level style prop that moved to a new defined
 *  value, unless pathOptions sets that key itself. A top-level prop that
 *  becomes undefined leaves the style as it is. className and the other
 *  options never restyle: Leaflet reads them only when it builds the element. */
export function restyle(path: Path, next: PathProps, prev: PathProps): void {
  const own = styleOf(next.pathOptions);
  const style = next.pathOptions !== prev.pathOptions ? own : {};
  for (const key of STYLE_KEYS) {
    if (next[key] !== prev[key] && next[key] !== undefined && !(key in own)) style[key] = next[key];
  }
  if (Object.keys(style).length > 0) path.setStyle(style);
}

/** Binds the handlers while they are the same object; a new object unbinds
 *  the old handlers before the new ones are bound. */
export function useEvents(target: Evented, handlers: LeafletEventHandlerFnMap | undefined): void {
  useLayoutEffect(() => {
    if (!handlers) return undefined;
    target.on(handlers);
    return () => {
      target.off(handlers);
    };
  }, [target, handlers]);
}

/**
 * A layer that lives as long as the component: built once from the first
 * props, on the map while mounted, and told about later prop changes through
 * `update`, which compares them with the props it last applied. Those keep
 * a copy of their coordinates, so a caller's array mutated in place still
 * counts as a move.
 *
 * The layer is built during render rather than in an effect. A Leaflet layer
 * touches nothing until it is added, and React runs a child's effects before
 * its parent's, so a Popup or Tooltip inside finds the layer it binds to.
 */
export function useLayer<T extends Layer, P extends LayerProps>(
  props: P,
  ref: ForwardedRef<T>,
  create: (props: P) => T,
  update: (layer: T, next: P, prev: P) => void,
) {
  const { map } = useMapContext();
  const [layer] = useState(() => create(props));
  const applied = useRef<P | null>(null);
  useEvents(layer, props.eventHandlers);
  useLayoutEffect(() => {
    map.addLayer(layer);
    return () => {
      map.removeLayer(layer);
    };
  }, [map, layer]);
  useLayoutEffect(() => {
    if (applied.current) update(layer, props, applied.current);
    const copy: Record<string, unknown> = { ...(props as object) };
    for (const key of COORDINATE_PROPS) if (key in copy) copy[key] = copyCoords(copy[key]);
    applied.current = copy as P;
  });
  useImperativeHandle(ref, () => layer, [layer]);
  const context = useMemo(() => ({ map, layer }), [map, layer]);
  return <MapContext.Provider value={context}>{props.children}</MapContext.Provider>;
}

/**
 * A Popup or Tooltip bound to the enclosing layer. Its children render
 * through a portal into the element Leaflet shows as the overlay's content,
 * and only once the overlay first opens: until then none of them exists.
 * A permanent tooltip is open from the start, so it renders them at once.
 * Once rendered they stay, and the overlay reopens with them.
 *
 * They render as Leaflet starts to open the overlay, before it builds and
 * measures it, so its first size and the pan that brings it into view fit
 * the content. Opened from inside a React render or effect, where React
 * cannot render on the spot, the content follows at the end of that commit
 * and the overlay is re-measured then.
 *
 * Later changes edit the content where it stands. An open overlay is then
 * re-measured, re-positioned and panned around it without moving it, so
 * focus, a text selection or a drag inside it survive.
 */
export function useOverlay<T extends DivOverlay, P extends LayerProps>(
  props: P,
  ref: ForwardedRef<T>,
  create: (props: P) => T,
  attach: (layer: Layer, overlay: T) => () => void,
) {
  const { layer } = useMapContext();
  const { children } = props;
  const [[overlay, content]] = useState(() => [create(props), document.createElement("div")] as const);
  const [rendered, setRendered] = useState(() => (overlay.options as { permanent?: boolean }).permanent === true);
  useEvents(overlay, props.eventHandlers);
  useLayoutEffect(() => {
    // Leaflet calls beforeAdd as the overlay starts to open, before it
    // builds the overlay, puts its content in place and measures it.
    overlay.beforeAdd = () => {
      if (!rendered) flushSync(() => setRendered(true));
      overlay.setContent(content);
      return overlay;
    };
    return () => {
      delete overlay.beforeAdd;
    };
  }, [overlay, content, rendered]);
  useLayoutEffect(() => (layer ? attach(layer, overlay) : undefined), [layer, overlay, attach]);
  useLayoutEffect(() => {
    // update() would empty the content node and insert the content again,
    // detaching it; holding no content, it skips that step and only
    // re-measures, re-positions and pans. beforeAdd hands it back on reopen.
    if (overlay.isOpen()) overlay.setContent("");
  }, [overlay, children, rendered]);
  useImperativeHandle(ref, () => overlay, [overlay]);
  return rendered ? createPortal(children, content) : null;
}
