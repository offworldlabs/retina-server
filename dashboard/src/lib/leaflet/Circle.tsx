import L, {
  type Circle as LeafletCircle,
  type CircleMarker as LeafletCircleMarker,
  type CircleMarkerOptions,
  type LatLngExpression,
} from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, restyle, sameCoords, useLayer, type PathProps } from "./core";

export interface CircleProps extends CircleMarkerOptions, PathProps {
  center: LatLngExpression;
}

/** Shared with CircleMarker, whose radius is in pixels where a Circle's is in metres. */
export function updateCircle(circle: LeafletCircleMarker, next: CircleProps, prev: CircleProps) {
  if (!sameCoords(next.center, prev.center)) circle.setLatLng(next.center);
  if (next.radius !== prev.radius && next.radius !== undefined) circle.setRadius(next.radius);
  restyle(circle, next, prev);
}

const create = (props: CircleProps) => L.circle(props.center, leafletOptions(props, "center"));

/** A circle whose radius is in metres. */
export const Circle = forwardRef<LeafletCircle, CircleProps>(function Circle(props, ref) {
  return useLayer(props, ref, create, updateCircle);
});
