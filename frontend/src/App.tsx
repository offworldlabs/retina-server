import { useState, lazy, Suspense } from "react";
import PhysicsSettings from "./components/PhysicsSettings";
import { usesRealOnlyFeed } from "./utils/domains";
import { MapThemeProvider } from "./components/map/useMapTheme";

// Leaflet is ~300 KB — only load it when the Live Radar tab is first opened
const LiveAircraftMap = lazy(() => import("./components/LiveAircraftMap"));
// Toy radar sim at /test-radar — dev/flag-gated so it does NOT ship to prod.
// Without the gate this would bundle into every build.
const TestRadar = lazy(() => import("./components/TestRadar"));

// Toggle for /test-radar route. import.meta.env.DEV covers `vite dev`; the
// VITE_ENABLE_TEST_RADAR flag lets us enable it on staging if needed.
const TEST_RADAR_ENABLED =
  import.meta.env.DEV || import.meta.env.VITE_ENABLE_TEST_RADAR === "1";
const IS_TEST_RADAR_ROUTE =
  typeof window !== "undefined" && window.location.pathname === "/test-radar";

export default function App() {
  if (IS_TEST_RADAR_ROUTE && TEST_RADAR_ENABLED) {
    return (
      <Suspense fallback={<div style={{ padding: 24, color: "#94a3b8" }}>Loading test radar…</div>}>
        <TestRadar />
      </Suspense>
    );
  }
  // The theme owns the palette the whole surface is drawn with, so it wraps
  // everything rather than sitting inside the map: the Physics tab needs it
  // too, and the token block it stamps is on the surface element above both.
  return (
    <MapThemeProvider>
      <MainApp />
    </MapThemeProvider>
  );
}

function MainApp() {
  const [activeTab, setActiveTab] = useState("live");

  return (
    <div className="app map-surface">
      <header className="app-header">
        <span className="header-icon">&#9041;</span>
        <h1>RETINA</h1>
        <span className="subtitle">Passive Radar Live Map</span>
        <nav className="header-tabs">
          {!usesRealOnlyFeed && (
            <button
              className={`tab-btn ${activeTab === "physics" ? "active" : ""}`}
              onClick={() => setActiveTab("physics")}
            >
              Physics Layer
            </button>
          )}
          <button
            className={`tab-btn ${activeTab === "live" ? "active" : ""}`}
            onClick={() => setActiveTab("live")}
          >
            Live Radar
          </button>
        </nav>
      </header>

      <main className={`app-body${activeTab === "live" ? " live-active" : ""}${activeTab === "physics" ? " physics-active" : ""}`}>
        {/* Hidden rather than unmounted when inactive, to preserve WebSocket state */}
        <Suspense fallback={<div style={{display:"flex",alignItems:"center",justifyContent:"center",height:"100%",color:"#94a3b8",fontSize:"0.9rem"}}>Loading map…</div>}>
          <div style={{ display: activeTab === "live" ? "contents" : "none" }}>
            <LiveAircraftMap />
          </div>
        </Suspense>
        {activeTab === "physics" && <PhysicsSettings />}
      </main>
    </div>
  );
}
