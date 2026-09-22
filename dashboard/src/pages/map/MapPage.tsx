import { useLocation } from "react-router-dom";
import ErrorBoundary from "../../components/ErrorBoundary";
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
  const { pathname } = useLocation();
  return (
    <MapThemeProvider>
      <MapSurface>
        {/* Inside the surface, so the fallback is drawn in the map's theme. Keyed
            on the path because /map and /sim render this one instance, and
            moving between them does not remount it. */}
        <ErrorBoundary
          resetKey={pathname}
          title="Something went wrong with the map"
          message="The address keeps your position, zoom and layers, so trying again returns you to the same view."
        >
          <LiveAircraftMap feed={feed} />
        </ErrorBoundary>
      </MapSurface>
    </MapThemeProvider>
  );
}
