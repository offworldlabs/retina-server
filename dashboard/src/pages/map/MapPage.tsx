import { MapThemeProvider } from "./useMapTheme";
import MapSurface from "./MapSurface";
import LiveAircraftMap from "./LiveAircraftMap";
import type { FeedMode } from "./feedMode";
import "./map-surface.css";

/**
 * `feed` names which fleet this page shows; unset, the page takes the
 * hostname's default (see feedMode.ts). /sim mounts it as "synthetic".
 */
export default function MapPage({ feed }: { feed?: FeedMode } = {}) {
  return (
    <MapThemeProvider>
      <MapSurface>
        <LiveAircraftMap feed={feed} />
      </MapSurface>
    </MapThemeProvider>
  );
}
