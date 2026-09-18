import { MapThemeProvider } from "./useMapTheme";
import MapSurface from "./MapSurface";
import PhysicsSettings from "./PhysicsSettings";
import "./map-surface.css";
import "./PhysicsSettings.css";

// The provider is what publishes the palette PhysicsSettings reads through
// usePalette. Without it a visitor who lands here directly, never having opened
// the map, is drawn in whatever palette the module happened to start with.
export default function PhysicsPage() {
  return (
    <MapThemeProvider>
      <MapSurface>
        <PhysicsSettings />
      </MapSurface>
    </MapThemeProvider>
  );
}
