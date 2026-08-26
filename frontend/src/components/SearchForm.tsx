import { useState, useEffect, useRef } from "react";
import { fetchElevation } from "../api";
import "./SearchForm.css";

export default function SearchForm({ onSearch, loading }) {
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [altitude, setAltitude] = useState("");
  const [source, setSource] = useState("auto");
  const [geoError, setGeoError] = useState(null);
  const [geoLoading, setGeoLoading] = useState(false);
  const altitudeManual = useRef(false);
  const [frequencies, setFrequencies] = useState([""]);
  const [showFrequencies, setShowFrequencies] = useState(false);

  // Auto-lookup elevation when lat/lon change and altitude hasn't been manually set
  useEffect(() => {
    if (altitudeManual.current) return;
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    if (isNaN(parsedLat) || isNaN(parsedLon)) return;

    // Debounced + aborted: this fired one un-cancellable request per
    // KEYSTROKE in the lat/lon inputs.
    const controller = new AbortController();
    const timer = setTimeout(() => {
      fetchElevation(parsedLat, parsedLon, controller.signal).then((elev) => {
        if (!controller.signal.aborted && elev != null && !altitudeManual.current) {
          setAltitude(Math.round(elev).toString());
        }
      }).catch(() => {});
    }, 400);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [lat, lon]);

  function handleSubmit(e) {
    e.preventDefault();
    const parsedLat = parseFloat(lat);
    const parsedLon = parseFloat(lon);
    if (isNaN(parsedLat) || isNaN(parsedLon)) return;
    const parsedFreqs = frequencies
      .map((f) => parseFloat(f))
      .filter((f) => !isNaN(f) && f > 0);
    onSearch({
      lat: parsedLat,
      lon: parsedLon,
      altitude: parseFloat(altitude) || 0,
      source,
      frequencies: parsedFreqs,
    });
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      setGeoError("Geolocation not supported by your browser");
      return;
    }
    setGeoError(null);
    setGeoLoading(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setGeoLoading(false);
        setLat(pos.coords.latitude.toFixed(6));
        setLon(pos.coords.longitude.toFixed(6));
        if (pos.coords.altitude != null) {
          setAltitude(Math.round(pos.coords.altitude).toString());
        }
      },
      (err) => {
        setGeoLoading(false);
        const msgs = {
          1: "Location access denied — please allow location in browser settings",
          2: "Location unavailable",
          3: "Location request timed out",
        };
        setGeoError(msgs[err.code] || err.message);
      },
      { timeout: 10000, maximumAge: 60000, enableHighAccuracy: false }
    );
  }

  return (
    <form className="search-form" onSubmit={handleSubmit}>
      <h2>Location</h2>

      <div className="field-row">
        <label>
          Latitude
          <input
            type="number"
            step="any"
            min={-90}
            max={90}
            value={lat}
            onChange={(e) => setLat(e.target.value)}
            placeholder="e.g. 38.8977"
            required
          />
        </label>
        <label>
          Longitude
          <input
            type="number"
            step="any"
            min={-180}
            max={180}
            value={lon}
            onChange={(e) => setLon(e.target.value)}
            placeholder="e.g. -77.0365"
            required
          />
        </label>
      </div>

      <div className="field-row">
        <label>
          Altitude (m)
          <input
            type="number"
            step="any"
            min={0}
            value={altitude}
            onChange={(e) => {
              setAltitude(e.target.value);
              if (e.target.value !== "") altitudeManual.current = true;
              else altitudeManual.current = false;
            }}
            placeholder="Auto-detected"
          />
        </label>
        <label>
          Data Source
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="auto">Auto-detect from coordinates</option>
            <option value="us">United States (FCC)</option>
            <option value="ca">Canada (ISED)</option>
            <option value="au">Australia (ACMA)</option>
          </select>
        </label>
      </div>

      <div className="freq-toggle">
        <button
          type="button"
          className="btn-link"
          onClick={() => setShowFrequencies(!showFrequencies)}
        >
          {showFrequencies ? "Hide" : "Add"} Measured Frequencies
        </button>
      </div>

      {showFrequencies && (
        <div className="freq-section">
          <label className="freq-label">Measured Frequencies (MHz)</label>
          <div className="freq-inputs">
            {frequencies.map((freq, i) => (
              <div key={i} className="freq-row">
                <input
                  type="number"
                  step="any"
                  min={0}
                  value={freq}
                  onChange={(e) => {
                    const updated = [...frequencies];
                    updated[i] = e.target.value;
                    setFrequencies(updated);
                  }}
                  placeholder={`Freq ${i + 1} (MHz)`}
                />
                {frequencies.length > 1 && (
                  <button
                    type="button"
                    className="btn-remove-freq"
                    onClick={() => setFrequencies(frequencies.filter((_, j) => j !== i))}
                    title="Remove"
                  >
                    &times;
                  </button>
                )}
              </div>
            ))}
          </div>
          {frequencies.length < 10 && (
            <button
              type="button"
              className="btn-add-freq"
              onClick={() => setFrequencies([...frequencies, ""])}
            >
              + Add Frequency
            </button>
          )}
        </div>
      )}

      <div className="form-actions">
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "Searching…" : "Find Towers"}
        </button>
        <button
          type="button"
          className="btn-secondary"
          onClick={useMyLocation}
          disabled={loading || geoLoading}
        >
          {geoLoading ? "Getting location…" : "Use My Location"}
        </button>
      </div>

      {geoError && <p className="geo-error">{geoError}</p>}
    </form>
  );
}
