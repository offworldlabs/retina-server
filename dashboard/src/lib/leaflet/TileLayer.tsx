import L, { type TileLayer as LeafletTileLayer, type TileLayerOptions } from "leaflet";
import { forwardRef } from "react";
import { leafletOptions, useLayer, type LayerProps } from "./core";

export interface TileLayerProps extends TileLayerOptions, LayerProps {
  url: string;
}

const create = (props: TileLayerProps) => L.tileLayer(props.url, leafletOptions(props, "url"));

function update(layer: LeafletTileLayer, next: TileLayerProps, prev: TileLayerProps) {
  if (next.url !== prev.url) layer.setUrl(next.url);
}

/** Only `url` follows the prop after mount; the other options are read once. */
export const TileLayer = forwardRef<LeafletTileLayer, TileLayerProps>(function TileLayer(props, ref) {
  return useLayer(props, ref, create, update);
});
