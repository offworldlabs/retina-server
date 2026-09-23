import ErrorBoundary from "../../components/ErrorBoundary";
import { MapThemeProvider } from "./useMapTheme";
import MapSurface from "./MapSurface";
import LiveAircraftMap from "./LiveAircraftMap";
import type { FeedMode } from "./feedMode";
import "./map-surface.css";

/**
 * `feed` names which fleet this page shows; unset, the page takes the
 * hostname's default (see feedMode.ts). The admin console's /sim mounts it as
 * "synthetic". `ownerView` offers a signed-in owner the map of just their own
 * nodes.
 */
export default function MapPage({ feed, ownerView = true }: { feed?: FeedMode; ownerView?: boolean } = {}) {
  return (
    <MapThemeProvider>
      <MapSurface>
        {/* Inside the surface, so the fallback is drawn in the map's theme. */}
        <ErrorBoundary
          title="Something went wrong with the map"
          message="The address keeps your position, zoom and layers, so trying again returns you to the same view."
        >
          <LiveAircraftMap feed={feed} ownerView={ownerView} />
        </ErrorBoundary>
      </MapSurface>
    </MapThemeProvider>
  );
}
