/**
 * The radius filter's centre, typed rather than clicked.
 *
 * The map is the quick way to place a centre and the only way to see one, but
 * a coordinate from somewhere else has to be enterable, and emptying the two
 * fields is how the filter is taken off again.
 */

import { useState } from "react";

import { DEFAULT_RADIUS_KM, MIN_RADIUS_KM, type ExplorerFilters } from "./urlState";

interface Props {
  filters: ExplorerFilters;
  /** The radius to use once there is a centre. `filters.near` owns it as soon
   *  as one exists; until then it has nowhere in the query string to live. */
  radiusKm: number;
  onRadiusChange: (km: number) => void;
  /** Whether the map is on screen, which this labels rather than owns. */
  mapOpen: boolean;
  onToggleMap: () => void;
  onChange: (next: ExplorerFilters) => void;
}

interface Draft {
  lat: string;
  lon: string;
  km: string;
}

const draftOf = (near: ExplorerFilters["near"], radiusKm: number): Draft => ({
  lat: near ? near.lat.toFixed(4) : "",
  lon: near ? near.lon.toFixed(4) : "",
  km: String(near ? near.km : radiusKm),
});

/** What the fields hold, as a value that only changes when the filter does.
 *  The filters object itself is rebuilt on every read of the query string. */
const signatureOf = (near: ExplorerFilters["near"]): string =>
  near ? `${near.lat},${near.lon},${near.km}` : "";

export function NearControls({
  filters,
  radiusKm,
  onRadiusChange,
  mapOpen,
  onToggleMap,
  onChange,
}: Props) {
  const signature = signatureOf(filters.near);
  const [draft, setDraft] = useState<Draft>(() => draftOf(filters.near, radiusKm));
  const [synced, setSynced] = useState(signature);

  // A centre placed on the map has to reach the fields. Adjusting state during
  // the render that saw the change, rather than in an effect, so the inputs
  // never paint one frame stale.
  if (signature !== synced) {
    setSynced(signature);
    setDraft(draftOf(filters.near, radiusKm));
  }

  // Deferred to blur, not applied per keystroke: half of "51.5" is a centre
  // in the Gulf of Guinea, and each commit rewrites the query string.
  const commit = (next: Draft) => {
    const lat = Number.parseFloat(next.lat);
    const lon = Number.parseFloat(next.lon);
    const km = Math.max(MIN_RADIUS_KM, Number.parseFloat(next.km) || DEFAULT_RADIUS_KM);
    // A radius with no centre to sit at is still an answer to "how far": it is
    // held until a click on the map gives it somewhere to be.
    if (km !== radiusKm) onRadiusChange(km);
    const near =
      Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon, km } : null;
    if (signatureOf(near) === signature) return;
    onChange({ ...filters, near });
  };

  const field = (key: keyof Draft) => ({
    value: draft[key],
    onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
      setDraft({ ...draft, [key]: e.target.value }),
    onBlur: () => commit(draft),
    onKeyDown: (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") commit(draft);
    },
  });

  return (
    <div className="de-fgroup">
      <label htmlFor="de-near-lat">Near (published node position)</label>
      <div className="de-frow">
        <input
          id="de-near-lat"
          type="number"
          step="0.0001"
          min={-90}
          max={90}
          placeholder="lat"
          aria-label="centre latitude"
          {...field("lat")}
        />
        <input
          type="number"
          step="0.0001"
          min={-180}
          max={180}
          placeholder="lon"
          aria-label="centre longitude"
          {...field("lon")}
        />
        <span className="de-muted" aria-hidden="true">±</span>
        <input
          type="number"
          className="de-km"
          min={MIN_RADIUS_KM}
          step="1"
          aria-label="radius in km"
          {...field("km")}
        />
        <span className="de-muted">km</span>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          aria-expanded={mapOpen}
          onClick={onToggleMap}
        >
          {mapOpen ? "Hide map" : "Map"}
        </button>
      </div>
    </div>
  );
}
