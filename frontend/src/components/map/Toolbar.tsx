import { useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * Two rows in one bar.
 *
 * Row one is state: is the feed live, how many tracks, and the three controls
 * that act on the view as a whole (pause, fit, and a menu holding the rest).
 * Row two is the layer switches, grouped under micro-labels by what they
 * change — what is drawn, what is analysed, how the view behaves.
 *
 * The grouping is the point. These were one flat line of twenty identical
 * buttons, which said nothing about which of them drew a polygon and which
 * filtered the fleet, and reflowed into an unpredictable number of rows as the
 * window narrowed. The low-traffic actions (locate, share, export, sound,
 * basemap, help) moved into the overflow menu for the same reason: they are
 * things you do once, not state you read.
 */
export default function Toolbar({
  connected,
  paused,
  aircraftCount,
  anomalyCount,
  showCoverage,
  showLabels,
  showTrails,
  showGroundTruth,
  showAnomaliesOnly,
  showIlluminators,
  colorByAlt,
  followSelected,
  showFilters,
  showStats,
  showRangeRings,
  showInBeamDiag,
  showArcs,
  showUncertainty,
  soundOn,
  tileTheme,
  hasUserLoc,
  filters,
  onFiltersChange,
  theme,
  onToggleTheme,
  onToggleCoverage,
  onToggleLabels,
  onToggleTrails,
  onToggleGroundTruth,
  onToggleAnomaliesOnly,
  onToggleIlluminators,
  onToggleColorByAlt,
  onToggleFollow,
  onToggleFilters,
  onToggleStats,
  onToggleRangeRings,
  onToggleInBeamDiag,
  onToggleArcs,
  onToggleUncertainty,
  onToggleSound,
  onCycleTheme,
  onShare,
  onLocate,
  onExportAll,
  onShowHelp,
  onTogglePause,
  onFit,
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const actionsRef = useRef<HTMLDivElement>(null);
  const filtersRef = useRef<HTMLDivElement>(null);

  // Both popovers close on an outside click, registered only while one is open
  // so the map keeps its own pointer handling the rest of the time. A click on
  // the owning button lands inside the ref and is left to that button's own
  // handler, which would otherwise toggle it straight back open.
  //
  // Escape closes the overflow menu only. The map already binds Escape to
  // "clear search / deselect aircraft", and both listeners sit on document, so
  // extending it to the filters would silently deselect the aircraft you were
  // looking at every time you dismissed the popover.
  useEffect(() => {
    if (!menuOpen && !showFilters) return;
    function onPointerDown(e: PointerEvent) {
      const t = e.target as Node;
      if (menuOpen && !actionsRef.current?.contains(t)) setMenuOpen(false);
      if (showFilters && !filtersRef.current?.contains(t)) onToggleFilters();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && menuOpen) setMenuOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen, showFilters, onToggleFilters]);

  // Menu entries run the action and close, so the menu never sits over the map
  // waiting to be dismissed.
  const run = (fn) => () => {
    setMenuOpen(false);
    fn();
  };

  const themeLabel =
    tileTheme === "positron" ? "Light" : tileTheme === "voyager" ? "Muted" : "OSM";

  return (
    <div className="live-map-toolbar">
      <div className="toolbar-row-status">
        <span className={`connection-badge ${connected ? "connected" : "disconnected"}`}>
          {connected ? (paused ? "PAUSED" : "LIVE") : "POLL"}
        </span>
        <span className="aircraft-count">{aircraftCount} aircraft</span>

        <div className="toolbar-spacer" />

        <div className="toolbar-actions" ref={actionsRef}>
          <button className={`toggle-btn${paused ? " active" : ""}`} onClick={onTogglePause}>
            {paused ? "▶ Resume" : "⏸ Pause"}
          </button>
          <button className="toggle-btn" onClick={onFit} title="Fit the view to everything on the map">
            ◎ Fit
          </button>
          <button
            className={`toggle-btn${menuOpen ? " active" : ""}`}
            onClick={() => setMenuOpen((v) => !v)}
            aria-expanded={menuOpen}
            aria-haspopup="menu"
            title="More actions"
          >
            ⋯
          </button>

          {menuOpen && (
            <div className="toolbar-menu" role="menu">
              <button
                role="menuitem"
                className={hasUserLoc ? "active" : ""}
                onClick={run(onLocate)}
              >
                Centre on my location <span className="menu-hint">m</span>
              </button>
              <button role="menuitem" onClick={run(onShare)}>
                Copy link to this view
              </button>
              <button role="menuitem" onClick={run(onExportAll)}>
                Export trails as CSV <span className="menu-hint">⇧X</span>
              </button>
              <button
                role="menuitem"
                className={soundOn ? "active" : ""}
                onClick={run(onToggleSound)}
              >
                Emergency squawk alert
                <span className="menu-hint">{soundOn ? "on" : "off"}</span>
              </button>
              <button
                role="menuitem"
                className={theme === "light" ? "active" : ""}
                onClick={run(onToggleTheme)}
              >
                Light mode <span className="menu-hint">{theme === "light" ? "on" : "off"}</span>
              </button>
              <button role="menuitem" onClick={run(onCycleTheme)}>
                Basemap <span className="menu-hint">{themeLabel}</span>
              </button>
              <button role="menuitem" onClick={run(onShowHelp)}>
                Keyboard shortcuts <span className="menu-hint">?</span>
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="toolbar-break" />

      <div className="toolbar-row-layers">
        <div className="toolbar-group">
          <span className="toolbar-group-label">Overlays</span>
          <button className={`toggle-btn${showCoverage ? " active" : ""}`} onClick={onToggleCoverage}>
            Coverage
          </button>
          <button
            className={`toggle-btn${showArcs ? " active" : ""}`}
            onClick={onToggleArcs}
            title="Show / hide bistatic detection arcs (d)"
          >
            Arcs
          </button>
          <button
            className={`toggle-btn${showIlluminators ? " active" : ""}`}
            onClick={onToggleIlluminators}
          >
            Illuminators
          </button>
          <button className={`toggle-btn${showTrails ? " active" : ""}`} onClick={onToggleTrails}>
            Trails
          </button>
          <button className={`toggle-btn${showLabels ? " active" : ""}`} onClick={onToggleLabels}>
            Labels
          </button>
        </div>

        <div className="toolbar-separator" />

        <div className="toolbar-group">
          <span className="toolbar-group-label">Analysis</span>
          <button
            className={`toggle-btn${showGroundTruth ? " active" : ""}`}
            onClick={onToggleGroundTruth}
            title="Overlay the ADS-B positions the solver is being measured against"
          >
            Debug Truth
          </button>
          <button
            className={`toggle-btn alert${showAnomaliesOnly ? " active" : ""}`}
            onClick={onToggleAnomaliesOnly}
            title="Show only tracks flagged as anomalous"
          >
            ⚠ Anomalies{anomalyCount > 0 ? ` (${anomalyCount})` : ""}
          </button>
          <button
            className={`toggle-btn${showInBeamDiag ? " active" : ""}`}
            onClick={onToggleInBeamDiag}
            title="Show red lines from a node to in-beam aircraft it is NOT currently detecting (beam-coverage gaps)"
          >
            Beam gaps
          </button>
          <button
            className={`toggle-btn${showUncertainty ? " active" : ""}`}
            onClick={onToggleUncertainty}
            title="Show the 68% position-uncertainty disc around multi-node solves (the panel also quotes 95%)"
          >
            σ Uncert.
          </button>
          <button
            className={`toggle-btn${showRangeRings ? " active" : ""}`}
            onClick={onToggleRangeRings}
            title="Show 5/10/20 km range rings around the selected aircraft"
          >
            Range rings
          </button>
          <button className={`toggle-btn${colorByAlt ? " active" : ""}`} onClick={onToggleColorByAlt}>
            Alt colour
          </button>
        </div>

        <div className="toolbar-separator" />

        <div className="toolbar-group">
          <span className="toolbar-group-label">View</span>
          <button className={`toggle-btn${followSelected ? " active" : ""}`} onClick={onToggleFollow}>
            Follow
          </button>
          <div className="toolbar-filters-anchor" ref={filtersRef}>
            <button
              className={`toggle-btn${showFilters ? " active" : ""}`}
              onClick={onToggleFilters}
              aria-expanded={showFilters}
            >
              Filters{activeFilterCount(filters) > 0 ? ` (${activeFilterCount(filters)})` : ""}
            </button>
            {showFilters && (
              <FiltersPopover
                filters={filters}
                onChange={onFiltersChange}
                anchorRef={filtersRef}
              />
            )}
          </div>
          <button
            className={`toggle-btn${showStats ? " active" : ""}`}
            onClick={onToggleStats}
            title="Show / hide the live stats panel (s)"
          >
            Stats
          </button>
        </div>
      </div>
    </div>
  );
}

/** How many of the four filter fields are actually narrowing the fleet. Shown
 *  on the button so a filter left on is visible without opening the popover —
 *  an empty map with a forgotten floor set was the usual way to lose ten
 *  minutes here. */
function activeFilterCount(filters) {
  if (!filters) return 0;
  let n = 0;
  if (filters.minFl !== "") n++;
  if (filters.maxFl !== "") n++;
  if (filters.minGs !== "") n++;
  if (filters.type !== "all") n++;
  return n;
}

const POPOVER_WIDTH = 232;
const VIEWPORT_MARGIN = 8;
const GAP = 6;

function FiltersPopover({ filters, onChange, anchorRef }) {
  const set = (patch) => onChange((f) => ({ ...f, ...patch }));

  // Placed against the button's measured position and clamped into the viewport
  // on BOTH axes, because the button moves: the layer row wraps on a narrow
  // window, so the View group can sit anywhere from the right edge to the left
  // one, and a short window leaves no room below it.
  //
  // Measuring the popover itself rather than assuming its size: its height
  // depends on content, and its width is capped by a max-width on a narrow
  // screen, so a hardcoded figure would clamp against the wrong number exactly
  // when the clamping matters.
  const popRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    const anchor = anchorRef?.current;
    if (!anchor) return;

    const place = () => {
      const a = anchor.getBoundingClientRect();
      const el = popRef.current;
      const w = el?.offsetWidth || POPOVER_WIDTH;
      const h = el?.offsetHeight || 0;
      const maxLeft = Math.max(VIEWPORT_MARGIN, window.innerWidth - w - VIEWPORT_MARGIN);
      // Below the button, or above it when below would overflow the bottom.
      const below = a.bottom + GAP;
      const top =
        below + h > window.innerHeight - VIEWPORT_MARGIN && a.top - GAP - h > VIEWPORT_MARGIN
          ? a.top - GAP - h
          : Math.min(below, Math.max(VIEWPORT_MARGIN, window.innerHeight - h - VIEWPORT_MARGIN));
      setPos({ top, left: Math.max(VIEWPORT_MARGIN, Math.min(a.left, maxLeft)) });
    };

    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    // The toolbar rewraps on its own — a filter count appearing on the button
    // widens it, which can move the whole View group to another line while the
    // popover is open. Watching the anchor catches that; a resize listener
    // cannot, because the window never changed.
    const ro =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(place) : null;
    ro?.observe(anchor);
    ro?.observe(document.documentElement);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      ro?.disconnect();
    };
  }, [anchorRef]);

  return (
    <div
      ref={popRef}
      className="filters-popover"
      // Hidden until measured, so it can never be seen at a placeholder
      // position — the failure mode of placing in an effect.
      style={pos ? { top: pos.top, left: pos.left } : { visibility: "hidden", top: 0, left: 0 }}
    >
      <div className="card-header">
        Filters
        <button
          className="filter-clear"
          onClick={() => onChange({ minFl: "", maxFl: "", minGs: "", type: "all" })}
        >
          Clear all
        </button>
      </div>
      <div className="card-body">
        <label className="filter-row">
          <span className="filter-label">Flight level</span>
          <span className="filter-inputs">
            <input
              type="number"
              placeholder="min"
              value={filters.minFl}
              onChange={(e) => set({ minFl: e.target.value })}
            />
            <span>–</span>
            <input
              type="number"
              placeholder="max"
              value={filters.maxFl}
              onChange={(e) => set({ maxFl: e.target.value })}
            />
          </span>
        </label>
        <label className="filter-row">
          <span className="filter-label">Min speed (kt)</span>
          <span className="filter-inputs">
            <input
              type="number"
              placeholder="0"
              value={filters.minGs}
              onChange={(e) => set({ minGs: e.target.value })}
            />
          </span>
        </label>
        <label className="filter-row">
          <span className="filter-label">Type</span>
          <span className="filter-inputs">
            <select value={filters.type} onChange={(e) => set({ type: e.target.value })}>
              <option value="all">All</option>
              <option value="aircraft">Aircraft</option>
              <option value="drone">Drones</option>
              <option value="multinode">Multi-node only</option>
            </select>
          </span>
        </label>
      </div>
    </div>
  );
}
