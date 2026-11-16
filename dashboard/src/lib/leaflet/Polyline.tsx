import L, { type LatLngExpression, type Polyline as LeafletPolyline, type PolylineOptions } from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, restyle, sameCoords, useLayer, type PathProps } from "./core";

export interface PolylineProps extends PolylineOptions, PathProps {
  positions: LatLngExpression[] | LatLngExpression[][];
}

type LineProps = PathProps & { positions: Parameters<LeafletPolyline["setLatLngs"]>[0] };

/** Shared with Polygon. */
export function updatePositions(line: LeafletPolyline<any>, next: LineProps, prev: LineProps) {
  if (!sameCoords(next.positions, prev.positions)) line.setLatLngs(next.positions);
  restyle(line, next, prev);
}

const create = (props: PolylineProps) => L.polyline(props.positions, leafletOptions(props, "positions"));

export const Polyline = forwardRef<LeafletPolyline, PolylineProps>(function Polyline(props, ref) {
  return useLayer(props, ref, create, updatePositions);
});
