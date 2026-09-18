import { MapThemeProvider } from "./useMapTheme";
import MapSurface from "./MapSurface";
import LiveAircraftMap from "./LiveAircraftMap";
import "./map-surface.css";

export default function MapPage() {
  return (
    <MapThemeProvider>
      <MapSurface>
        <LiveAircraftMap />
      </MapSurface>
    </MapThemeProvider>
  );
}
