import L, { type Layer, type Popup as LeafletPopup, type PopupOptions } from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, useOverlay, type LayerProps } from "./core";

export type PopupProps = PopupOptions & LayerProps;

const create = (props: PopupProps) => L.popup(leafletOptions(props));

function attach(layer: Layer, popup: LeafletPopup) {
  layer.bindPopup(popup);
  return () => {
    layer.unbindPopup();
    popup.close();
  };
}

/** A popup on the enclosing layer, opened by Leaflet when the layer is clicked. */
export const Popup = forwardRef<LeafletPopup, PopupProps>(function Popup(props, ref) {
  return useOverlay(props, ref, create, attach);
});
