import L, { type LatLngExpression, type Marker as LeafletMarker, type MarkerOptions } from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, sameCoords, useLayer, type LayerProps } from "./core";

export interface MarkerProps extends MarkerOptions, LayerProps {
  position: LatLngExpression;
}

const create = (props: MarkerProps) => L.marker(props.position, leafletOptions(props, "position"));

function update(marker: LeafletMarker, next: MarkerProps, prev: MarkerProps) {
  if (!sameCoords(next.position, prev.position)) marker.setLatLng(next.position);
  if (next.icon !== prev.icon && next.icon) marker.setIcon(next.icon);
  if (next.zIndexOffset !== prev.zIndexOffset) marker.setZIndexOffset(next.zIndexOffset ?? 0);
}

/** position, icon and zIndexOffset follow their props; the other options are read once. */
export const Marker = forwardRef<LeafletMarker, MarkerProps>(function Marker(props, ref) {
  return useLayer(props, ref, create, update);
});
