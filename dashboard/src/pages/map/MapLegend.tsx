import { useState, type KeyboardEvent } from "react";

import { useMediaQuery } from "../../hooks/useMediaQuery";
import { PHONE } from "../../styles/breakpoints";
import { altitudeLegend } from "./icons";
import { truthLegend } from "./truthColor";
import { usePalette } from "./useMapTheme";
import { useMapPreference } from "./useMapPreference";

const NO_TRUTH_CLASSES: ReadonlySet<string> = new Set();

/**
 * The key to the track colours, pinned to the bottom-left of the map.
 *
 * It used to ride at the end of the toolbar, where it shared a row with the
 * controls and moved every time that row rewrapped. A legend is part of the
 * picture, not part of the chrome, so it now sits on the map and collapses to
 * its header for anyone who has learnt the colours.
 *
 * On a phone the open key covers a good part of a map that small, so there it
 * is a chip, its header alone, shut on every visit whatever the wide screen's
 * stored choice. A tap opens the key as a card above the chip for a glance,
 * and a second tap or Escape shuts it. That glance is kept here and never
 * stored, so it leaves the wide screen's choice as it was.
 */
export default function MapLegend({ colorByAlt, showGroundTruth, truthClasses = NO_TRUTH_CLASSES, showIlluminators, hasPlayback = false, hasArchive = false }) {
  const palette = usePalette();
  const { ILLUMINATOR, LANE_MN_ADSB, LANE_MN_DARK, LANE_SOLVER_SEED, NODE } = palette;
  const [stored, setStored] = useMapPreference("legendOpen", true);
  const phone = useMediaQuery(PHONE);
  const [chipOpen, setChipOpen] = useState(false);
  // A window narrowed to a phone's width is a phone visit too.
  const [wasPhone, setWasPhone] = useState(phone);
  if (phone !== wasPhone) {
    setWasPhone(phone);
    setChipOpen(false);
  }
  const open = phone ? chipOpen : stored;
  const setOpen = phone ? setChipOpen : setStored;

  // Kept from the map, whose own Escape would also deselect the aircraft in
  // view.
  function onChipKey(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Escape" || !chipOpen) return;
    e.stopPropagation();
    setChipOpen(false);
  }

  return (
    <div
      className={`map-legend${phone ? " map-legend-chip" : ""}${hasArchive ? " with-archive" : hasPlayback ? " with-playback" : ""}`}
      onKeyDown={phone ? onChipKey : undefined}
    >
      <button
        className="map-legend-header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "Collapse legend" : "Show legend"}
      >
        {colorByAlt ? "Altitude" : "Track source"}
        {/* The chip's key opens upwards. */}
        <span aria-hidden="true">{open ? "▾" : phone ? "▴" : "▸"}</span>
      </button>

      {open && (
        <div className="map-legend-body">
          {colorByAlt ? (
            altitudeLegend().map(([c, lbl]) => <LegendItem key={lbl} color={c} label={lbl} />)
          ) : (
            <>
              <LegendItem color={LANE_SOLVER_SEED} label="Solver + ADS-B" />
              {/* Multi-node splits by lane, same shades getAircraftColor gives
                  the icons: a solve that carried a transponder tag vs a dark
                  one. */}
              <LegendItem color={LANE_MN_ADSB} label="MLAT + ADS-B" />
              <LegendItem color={LANE_MN_DARK} label="MLAT dark" />
              {/* Truth splits four ways: simulated vs live-feed (neutral vs
                  teal) and with vs without a transponder (bright vs dark),
                  the same resolver the dots are drawn with. Only the classes
                  on the map are keyed, since the fleet's mix varies. In
                  archive mode the truth is the archived ADS-B traffic. */}
              {showGroundTruth && truthLegend(palette, hasArchive).filter((t) => truthClasses.has(t.cls)).map((t) => (
                <LegendItem key={t.cls} color={t.color} label={t.label} />
              ))}
            </>
          )}
          <LegendItem color={NODE} label="Node" />
          {showIlluminators && <LegendItem color={ILLUMINATOR} label="Illuminator" />}
        </div>
      )}
    </div>
  );
}

function LegendItem({ color, label }) {
  return (
    <span className="legend-item">
      <span className="legend-dot" style={{ background: color }} /> {label}
    </span>
  );
}
