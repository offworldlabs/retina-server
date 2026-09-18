import { MapThemeProvider } from "./useMapTheme";
import LiveAircraftMap from "./LiveAircraftMap";
import "./map-surface.css";

export default function MapPage() {
  return (
    <MapThemeProvider>
      {/* `.app` is 100vh in the standalone bundle. Here the pane has already
          been sized by the layout, so the surface takes all of it and no more. */}
      <div className="app map-surface map-embedded">
        <LiveAircraftMap />
      </div>
    </MapThemeProvider>
  );
}
