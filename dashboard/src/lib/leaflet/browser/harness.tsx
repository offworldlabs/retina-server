import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { StrictMode, Suspense, createElement, createRef, useEffect, type ComponentType, type ReactNode, type RefObject } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { Circle } from "../Circle";
import { useMap } from "../core";
import { MapContainer } from "../MapContainer";
import { Marker } from "../Marker";
import { Popup } from "../Popup";
import { TileLayer } from "../TileLayer";

/** A React tree as data, so a test can describe it from outside the page:
 *  [component or tag, props, ...children]. pathOptions, eventHandlers and icon
 *  name a fixture, so one name is one object across renders; ref names a slot
 *  in `refs`. */
export type Scene = [string, Record<string, unknown>, ...(Scene | string)[]];

const refs: Record<string, RefObject<any>> = {};
const calls: string[] = [];
const hits: string[] = [];
const fixtures: Record<string, Record<string, unknown>> = {
  pathOptions: {
    red: { color: "#ff0000" },
    blue: { color: "#0000ff" },
    redClassed: { color: "#ff0000", className: "probe-dropped" },
    blueClassed: { color: "#0000ff", className: "probe-dropped" },
  },
  eventHandlers: { a: { click: () => hits.push("a") }, b: { click: () => hits.push("b") } },
  icon: { dot: L.divIcon({ className: "probe-icon" }) },
};

// Every Leaflet setter call made from outside Leaflet is logged as
// "<ref name>.<method>"; the setters Leaflet calls from inside one are not.
let depth = 0;
for (const layerClass of [L.Path, L.Polyline, L.CircleMarker, L.Circle, L.Marker, L.GridLayer, L.TileLayer]) {
  const proto = layerClass.prototype as unknown as Record<string, (...args: unknown[]) => unknown>;
  for (const name of ["setStyle", "setLatLng", "setLatLngs", "setRadius", "setIcon", "setZIndexOffset", "setUrl", "setOpacity"]) {
    if (!Object.prototype.hasOwnProperty.call(proto, name)) continue;
    const original = proto[name];
    proto[name] = function (this: unknown, ...args: unknown[]) {
      if (depth === 0) calls.push(`${Object.keys(refs).find((key) => refs[key].current === this) ?? "?"}.${name}`);
      depth += 1;
      try {
        return original.apply(this, args);
      } finally {
        depth -= 1;
      }
    };
  }
}

/** Publishes the map and counts its effect mounts, which StrictMode doubles. */
function MapProbe() {
  const map = useMap();
  useEffect(() => {
    lf.mounts += 1;
    lf.map = map;
  }, [map]);
  return null;
}

/** Stands for the page around a map: its own effect, mounted in the same
 *  commit as the map, makes React run StrictMode's second mount of the map in
 *  its passive phase, at a lower priority than the first. */
function Page({ children }: { children?: ReactNode }) {
  useEffect(() => undefined, []);
  return <>{children}</>;
}

const never = new Promise<never>(() => undefined);

/** Suspends while `pending`, so the nearest Suspense boundary hides what it holds. */
function Suspender({ pending }: { pending?: boolean }) {
  if (pending) throw never;
  return null;
}

function build(node: Scene | string): ReactNode {
  if (typeof node === "string") return node;
  const [type, props, ...children] = node;
  const resolved: Record<string, unknown> = { ...props };
  for (const [prop, table] of Object.entries(fixtures)) {
    if (typeof props[prop] === "string") resolved[prop] = table[props[prop] as string];
  }
  if (typeof props.ref === "string") resolved.ref = refs[props.ref] = refs[props.ref] ?? createRef();
  return createElement(lf.types[type] ?? type, resolved, ...children.map(build));
}

const root = createRoot(document.getElementById("root"));

const lf = {
  L,
  types: { MapContainer, TileLayer, Marker, Popup, Circle, MapProbe, Page, Suspense, Suspender } as Record<string, ComponentType<any>>,
  refs,
  calls,
  hits,
  mounts: 0,
  map: null as L.Map | null,
  /** Renders synchronously, effects included; null unmounts everything. */
  render(scene: Scene | null, strict = false) {
    const tree = scene && build(scene);
    flushSync(() => root.render(strict ? <StrictMode>{tree}</StrictMode> : tree));
  },
};

export type Harness = typeof lf;

declare global {
  interface Window {
    lf: Harness;
  }
}

window.lf = lf;
