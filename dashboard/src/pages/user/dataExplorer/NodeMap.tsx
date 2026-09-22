/**
 * Where the fleet is, and the radius filter drawn on top of it.
 *
 * Clicking is the quick way to place the centre: there is no geocoder and no
 * tile source to search, so the nodes themselves are the landmarks and the
 * graticule is the only other reference on the page.
 */

import { useMemo, useState } from "react";
import type { MouseEvent } from "react";

import { placeLabels } from "./mapLabels";
import {
  centreExtent,
  fitExtent,
  gridLines,
  gridStep,
  project,
  ringRadii,
  unproject,
  VIEWPORT,
  type MapMode,
} from "./mapProjection";
import { distanceKm } from "../../../utils/geo";
import type { RegistryNode } from "./nodes";
import type { ExplorerFilters } from "./urlState";

/** Below this the ring is a dot and says nothing about where its edge is. */
const MIN_RING = 6;

/** Half the crosshair's arm. */
const PIN = 7;

interface Props {
  filters: ExplorerFilters;
  nodes: Map<string, RegistryNode>;
  /** Which nodes survive every filter, so the drawing agrees with the list. */
  effective: Set<string>;
  /** What a centre placed here starts with, until `filters.near` holds it. */
  radiusKm: number;
  /** The fleet has not arrived yet, which is not the same as it having no
   *  positions to draw. */
  loading?: boolean;
  onChange: (next: ExplorerFilters) => void;
}

const decimals = (step: number) => (step < 1 ? 1 : 0);

export function NodeMap({ filters, nodes, effective, radiusKm, loading, onChange }: Props) {
  const [mode, setMode] = useState<MapMode>("fit");

  const all = useMemo(() => Array.from(nodes.values()), [nodes]);
  const placed = useMemo(() => all.filter((n) => n.lat !== null && n.lon !== null), [all]);
  const unplaced = all.length - placed.length;
  const { near } = filters;

  // Only worth telling real from synthetic when both are on the map, and the
  // answer can flip after first paint as the fleet arrives.
  const anySynthetic = placed.some((n) => n.synthetic);
  const uncertainties = placed.map((n) => n.uncertaintyKm).filter((u) => u !== null);

  const fitted = useMemo(() => fitExtent(all), [all]);
  // A centred view needs a centre; without one the chip is off as well as
  // disabled, so what it claims and what is drawn cannot disagree.
  const centred = mode === "centre" && near !== null;
  const extent = centred ? centreExtent(near!.lat, near!.lon, near!.km) : fitted;

  const setNear = (next: ExplorerFilters["near"]) => onChange({ ...filters, near: next });

  // Placing a centre is also a request to look at it: a click that left the
  // view fitted to the whole fleet would put the ring somewhere off-screen.
  const centreOn = (lat: number, lon: number) => {
    setMode("centre");
    setNear({ lat, lon, km: near?.km ?? radiusKm });
  };

  const labels = useMemo(() => {
    if (!extent) return [];
    return placeLabels(
      placed.map((n) => {
        const { x, y } = project(n.lat!, n.lon!, extent);
        return { id: n.id, text: n.id, x, y };
      }),
    );
  }, [extent, placed]);

  if (!extent) {
    return (
      <div className="de-map de-map-empty">
        <p>
          {loading
            ? "Loading the fleet…"
            : "No node publishes a position, so there is nothing to draw."}
        </p>
      </div>
    );
  }

  const onMapClick = (event: MouseEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    // Zero before layout, and a click at that point would mean nothing.
    if (!box.width || !box.height) return;
    const x = ((event.clientX - box.left) / box.width) * VIEWPORT.width;
    const y = ((event.clientY - box.top) / box.height) * VIEWPORT.height;
    const { lat, lon } = unproject(x, y, extent);
    centreOn(lat, lon);
  };

  const lonStep = gridStep(extent.maxLon - extent.minLon);
  const latStep = gridStep(extent.maxLat - extent.minLat);

  const titleFor = (n: RegistryNode) => {
    const unc = n.uncertaintyKm === null ? "?" : n.uncertaintyKm;
    const from = near
      ? `\n${distanceKm(near.lat, near.lon, n.lat!, n.lon!).toFixed(1)} km from centre`
      : "";
    return `${n.id}\n${n.lat!.toFixed(4)}, ${n.lon!.toFixed(4)} (published, ±${unc} km)${from}`;
  };

  return (
    <div className="de-map">
      <div className="de-map-wrap">
        <svg
          className="de-map-svg"
          viewBox={`0 0 ${VIEWPORT.width} ${VIEWPORT.height}`}
          onClick={onMapClick}
        >
          <title>The fleet, and the area the radius filter covers</title>

          {gridLines(extent.minLon, extent.maxLon, lonStep).map((lon) => {
            const { x } = project(extent.minLat, lon, extent);
            return (
              <g key={`lon${lon}`}>
                <line className="de-map-grid" x1={x} y1={0} x2={x} y2={VIEWPORT.height} />
                <text className="de-map-gridlab" x={x + 3} y={VIEWPORT.height - 4}>
                  {lon.toFixed(decimals(lonStep))}°
                </text>
              </g>
            );
          })}
          {gridLines(extent.minLat, extent.maxLat, latStep).map((lat) => {
            const { y } = project(lat, extent.minLon, extent);
            return (
              <g key={`lat${lat}`}>
                <line className="de-map-grid" x1={0} y1={y} x2={VIEWPORT.width} y2={y} />
                <text className="de-map-gridlab" x={3} y={y - 3}>
                  {lat.toFixed(decimals(latStep))}°
                </text>
              </g>
            );
          })}

          {near &&
            (() => {
              const { x, y } = project(near.lat, near.lon, extent);
              const { rx, ry } = ringRadii(near.lat, near.km, extent);
              return (
                <g className="de-map-centre">
                  <ellipse
                    className="de-map-ring"
                    cx={x}
                    cy={y}
                    rx={Math.max(rx, MIN_RING)}
                    ry={Math.max(ry, MIN_RING)}
                  />
                  <line className="de-map-pin" x1={x - PIN} y1={y} x2={x + PIN} y2={y} />
                  <line className="de-map-pin" x1={x} y1={y - PIN} x2={x} y2={y + PIN} />
                </g>
              );
            })()}

          {placed.map((n) => {
            const { x, y } = project(n.lat!, n.lon!, extent);
            const inside = effective.has(n.id);
            const kind = anySynthetic && n.synthetic ? " de-map-node-synth" : "";
            return (
              <g key={n.id}>
                {near && inside && <circle className="de-map-selected" cx={x} cy={y} r={9} />}
                <circle
                  className={`de-map-node${kind}${inside ? "" : " de-map-faded"}`}
                  cx={x}
                  cy={y}
                  r={5}
                  role="button"
                  tabIndex={0}
                  aria-label={`Centre on ${n.name}`}
                  onClick={(event) => {
                    // Without this the map's own handler re-centres on the
                    // pixel under the marker instead of on the node.
                    event.stopPropagation();
                    centreOn(n.lat!, n.lon!);
                  }}
                  onKeyDown={(event) => {
                    // An SVG element synthesises no click from a key press, so
                    // without this the marker takes focus and cannot be used.
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    event.stopPropagation();
                    centreOn(n.lat!, n.lon!);
                  }}
                >
                  <title>{titleFor(n)}</title>
                </circle>
              </g>
            );
          })}

          {labels.map((l) => (
            <text
              key={l.id}
              className={`de-map-nlab${effective.has(l.id) ? "" : " de-map-faded"}`}
              x={l.labelX}
              y={l.labelY}
            >
              {l.text}
            </text>
          ))}
        </svg>
      </div>

      <div className="de-map-foot">
        <span>
          <b>Published</b> receiver positions, fuzzed by up to{" "}
          {uncertainties.length ? Math.max(...(uncertainties as number[])) : "—"} km
          (<code className="mono">location_uncertainty_km</code>); keep the radius well above it.
        </span>
        <span>Click the map to set the centre · click a node to centre on it.</span>
        <span className="de-chipset">
          <button
            type="button"
            className={`de-chip${centred ? "" : " on"}`}
            aria-pressed={!centred}
            onClick={() => setMode("fit")}
          >
            Fit all nodes
          </button>
          <button
            type="button"
            className={`de-chip${centred ? " on" : ""}`}
            aria-pressed={centred}
            disabled={!near}
            onClick={() => setMode("centre")}
          >
            Around centre
          </button>
        </span>
        <span>Nodes with no published position never match a location filter.</span>
        {unplaced > 0 && (
          <span>
            {unplaced} node{unplaced === 1 ? "" : "s"} without a published position.
          </span>
        )}
      </div>
    </div>
  );
}
