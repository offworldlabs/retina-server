import L, { type Layer, type Tooltip as LeafletTooltip, type TooltipOptions } from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, useOverlay, type LayerProps } from "./core";

export type TooltipProps = TooltipOptions & LayerProps;

const create = (props: TooltipProps) => L.tooltip(leafletOptions(props));

function attach(layer: Layer, tooltip: LeafletTooltip) {
  layer.bindTooltip(tooltip);
  return () => {
    layer.unbindTooltip();
  };
}

/** A tooltip on the enclosing layer: shown on hover, or always when `permanent`. */
export const Tooltip = forwardRef<LeafletTooltip, TooltipProps>(function Tooltip(props, ref) {
  return useOverlay(props, ref, create, attach);
});
