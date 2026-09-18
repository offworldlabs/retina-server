import { MapThemeProvider } from "./useMapTheme";
import LiveAircraftMap from "./LiveAircraftMap";
import "./map-surface.css";

export default function MapPage() {
  return (
    <MapThemeProvider>
      <div className="app map-surface">
        <LiveAircraftMap />
      </div>
    </MapThemeProvider>
  );
}
