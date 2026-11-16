import L, { type LatLngExpression, type Polygon as LeafletPolygon, type PolylineOptions } from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, useLayer, type PathProps } from "./core";
import { updatePositions } from "./Polyline";

export interface PolygonProps extends PolylineOptions, PathProps {
  positions: LatLngExpression[] | LatLngExpression[][] | LatLngExpression[][][];
}

const create = (props: PolygonProps) => L.polygon(props.positions, leafletOptions(props, "positions"));

export const Polygon = forwardRef<LeafletPolygon, PolygonProps>(function Polygon(props, ref) {
  return useLayer(props, ref, create, updatePositions);
});
