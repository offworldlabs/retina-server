import L, { type CircleMarker as LeafletCircleMarker } from "leaflet";
import { forwardRef } from "react";
import { updateCircle, type CircleProps } from "./Circle";
import { leafletOptions, useLayer } from "./core";

export type CircleMarkerProps = CircleProps;

const create = (props: CircleMarkerProps) => L.circleMarker(props.center, leafletOptions(props, "center"));

/** A circle whose radius is in screen pixels. */
export const CircleMarker = forwardRef<LeafletCircleMarker, CircleMarkerProps>(function CircleMarker(props, ref) {
  return useLayer(props, ref, create, updateCircle);
});
