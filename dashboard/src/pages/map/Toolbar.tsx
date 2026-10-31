import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { useMediaQuery } from "../../hooks/useMediaQuery";
import { TABLET } from "../../styles/breakpoints";
import { PLANE_PATH } from "./icons";
import LayersSheet, { type LayerGroup } from "./LayersSheet";
import { AT_LIVE_EDGE_TITLE } from "./replay/useArchiveReplay";

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
 *
 * Below desktop width the second row does not fit. On a phone its groups ran
 * off the right edge where nothing could reach them, and on a touch tablet the
 * finger-sized buttons wrapped it onto four rows. There the bar is one row (the
 * feed, the count, pause, fit, Layers and the menu) and Layers opens a sheet
 * holding the switches and the filters. The row and the sheet are drawn from
 * one list of switches built from the props below, so they cannot disagree:
 * whatever flips a switch, here or by a shortcut, shows in both, and the only
 * state of their own is whether the sheet is open. Filters is the switch that
 * opens a panel rather than drawing a layer, and in the compact bar that panel
 * is the sheet: opening it opens the sheet, a window narrowed with it open
 * opens the sheet too, and shutting the sheet shuts it.
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
  truthAvailable,
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
  archive = false,
  atLiveEdge = false,
  onOpenArchive = undefined,
}) {
  const compact = useMediaQuery(TABLET);
  const [menuOpen, setMenuOpen] = useState(false);
  // In either form the filter panel is on screen exactly while showFilters is
  // true: the popover at desktop width, and the sheet below it, which Layers
  // also opens without it. So the sheet opens at its first filter field
  // whenever the panel comes on here: opened by the map's `f` or anything
  // else, carried in by a window narrowing with the popover open, or already
  // on as the bar mounts. A key that toggles the panel always has one to show
  // or to shut.
  const [layersOpen, setLayersOpen] = useState(compact && showFilters);
  const [focusFilters, setFocusFilters] = useState(compact && showFilters);
  const [wasCompact, setWasCompact] = useState(compact);
  const [filtersWere, setFiltersWere] = useState(showFilters);
  if (compact !== wasCompact || showFilters !== filtersWere) {
    setWasCompact(compact);
    setFiltersWere(showFilters);
    if (compact && showFilters) {
      // One of the two has just turned true, so the panel has just come here.
      setMenuOpen(false);
      setLayersOpen(true);
      setFocusFilters(true);
    } else if (compact !== wasCompact) {
      // The sheet belongs to the compact bar, and a window widened past it
      // takes the sheet away with the bar. A panel still open is the popover
      // there, and a sheet Layers opened does not come back as it narrows.
      setLayersOpen(false);
    }
  }
  const actionsRef = useRef<HTMLDivElement>(null);
  const filtersRef = useRef<HTMLDivElement>(null);
  const firstFilterRef = useRef<HTMLInputElement>(null);

  // The filters popover hangs off the layer row, which the compact bar does
  // not draw: there the same filters sit inline in the sheet. A popover open
  // as the window narrows becomes the sheet (above), and its outside press
  // stops listening with it, so a press inside the sheet shuts nothing.
  const popoverOpen = showFilters && !compact;

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
    if (!menuOpen && !popoverOpen) return;
    function onPointerDown(e: PointerEvent) {
      const t = e.target as Node;
      if (menuOpen && !actionsRef.current?.contains(t)) setMenuOpen(false);
      if (popoverOpen && !filtersRef.current?.contains(t)) onToggleFilters();
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
  }, [menuOpen, popoverOpen, onToggleFilters]);

  // The menu and the sheet never stand open together: opening one shuts the
  // other.
  const toggleMenu = () => {
    setMenuOpen((v) => !v);
    setLayersOpen(false);
  };
  const openLayers = () => {
    setMenuOpen(false);
    setLayersOpen(true);
    setFocusFilters(false);
  };
  // Shutting the sheet shuts the filter panel with it, so none is left open
  // out of sight to pop up as the window widens.
  const closeLayers = () => {
    setLayersOpen(false);
    if (showFilters) onToggleFilters();
  };

  // Menu entries run the action and close, so the menu never sits over the map
  // waiting to be dismissed.
  const run = (fn) => () => {
    setMenuOpen(false);
    fn();
  };

  const themeLabel =
    tileTheme === "positron" ? "Light" : tileTheme === "voyager" ? "Muted" : "OSM";

  // A paused replay at the live edge has nothing to play; Jump to now goes further.
  const playBlocked = archive && paused && atLiveEdge;

  const filterCount = activeFilterCount(filters);

  // Every layer switch, once, for the layer row and the sheet alike. A switch
  // with no title has none in either.
  const layers: LayerGroup[] = [
    {
      label: "Overlays",
      switches: [
        { id: "coverage", label: "Coverage", on: showCoverage, onToggle: onToggleCoverage },
        {
          id: "arcs",
          label: "Arcs",
          title: "Show / hide bistatic detection arcs (d)",
          on: showArcs,
          onToggle: onToggleArcs,
        },
        { id: "illuminators", label: "Illuminators", on: showIlluminators, onToggle: onToggleIlluminators },
        { id: "trails", label: "Trails", on: showTrails, onToggle: onToggleTrails },
        { id: "labels", label: "Labels", on: showLabels, onToggle: onToggleLabels },
      ],
    },
    {
      label: "Analysis",
      switches: [
        // Only while there is a truth feed to draw, which in the archive is
        // the archived ADS-B traffic.
        ...(truthAvailable
          ? [
              {
                id: "truth",
                label: archive ? "ADS-B traffic" : "Debug Truth",
                title: archive
                  ? "Overlay the archived ADS-B traffic"
                  : "Overlay the ADS-B positions the solver is being measured against",
                on: showGroundTruth,
                onToggle: onToggleGroundTruth,
              },
            ]
          : []),
        {
          id: "anomalies",
          label: `⚠ Anomalies${anomalyCount > 0 ? ` (${anomalyCount})` : ""}`,
          title: "Show only tracks flagged as anomalous",
          alert: true,
          on: showAnomaliesOnly,
          onToggle: onToggleAnomaliesOnly,
        },
        {
          id: "coverage-gaps",
          label: "Coverage gaps",
          title:
            "Show red lines from a node to aircraft inside its measured coverage that it is NOT currently detecting (coverage gaps)",
          on: showInBeamDiag,
          onToggle: onToggleInBeamDiag,
        },
        {
          id: "uncertainty",
          label: "σ Uncert.",
          title: "Show the 68% position-uncertainty disc around multi-node solves (the panel also quotes 95%)",
          on: showUncertainty,
          onToggle: onToggleUncertainty,
        },
        {
          id: "range-rings",
          label: "Range rings",
          title: "Show 5/10/20 km range rings around the selected aircraft",
          on: showRangeRings,
          onToggle: onToggleRangeRings,
        },
        { id: "alt-colour", label: "Alt colour", on: colorByAlt, onToggle: onToggleColorByAlt },
      ],
    },
    {
      label: "View",
      switches: [
        { id: "follow", label: "Follow", on: followSelected, onToggle: onToggleFollow },
        {
          id: "filters",
          label: `Filters${filterCount > 0 ? ` (${filterCount})` : ""}`,
          opensFilters: true,
          on: showFilters,
          onToggle: onToggleFilters,
        },
        {
          id: "stats",
          label: "Stats",
          title: "Show / hide the live stats panel (s)",
          on: showStats,
          onToggle: onToggleStats,
        },
      ],
    },
  ];

  /* Archive outranks the connection: a replay is not the feed, whatever
     state the socket behind it is in. */
  const pill = (
    <span
      className={`badge connection-badge ${archive ? "archive" : connected ? "online" : "warning"}`}
    >
      {archive ? "ARCHIVE" : connected ? (paused ? "PAUSED" : "LIVE") : "POLL"}
    </span>
  );

  const more = (
    <button
      className="btn btn-secondary toggle-btn"
      onClick={toggleMenu}
      aria-expanded={menuOpen}
      aria-haspopup="menu"
      aria-label="More actions"
      title="More actions"
    >
      ⋯
    </button>
  );

  const menu = menuOpen && (
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
      {onOpenArchive && !archive && (
        <button role="menuitem" onClick={run(onOpenArchive)}>
          Open archive <span className="menu-hint">1 h ago</span>
        </button>
      )}
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
      <button role="menuitem" onClick={run(onCycleTheme)}>
        Basemap <span className="menu-hint">{themeLabel}</span>
      </button>
      <button role="menuitem" onClick={run(onShowHelp)}>
        Keyboard shortcuts <span className="menu-hint">?</span>
      </button>
    </div>
  );

  if (compact) {
    // Named by aria-label, since a phone shows these as icons alone; the
    // words beside the icons are for a tablet, which has the room.
    const pauseName = archive ? (paused ? "Play replay" : "Pause replay") : paused ? "Resume" : "Pause";
    const layersName =
      filterCount > 0 ? `Layers, ${filterCount} filter${filterCount === 1 ? "" : "s"} on` : "Layers";
    return (
      <div className="live-map-toolbar toolbar-compact">
        <div className="toolbar-row-status">
          {pill}
          {/* A phone keeps the number beside a plane and hides the word, which
              a screen reader still reads. */}
          <span className="aircraft-count">
            <svg className="aircraft-count-icon" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
              <path d={PLANE_PATH} />
            </svg>
            {aircraftCount}
            <span className="aircraft-count-unit"> aircraft</span>
          </span>

          <div className="toolbar-actions" ref={actionsRef}>
            <button
              className="btn btn-secondary toggle-btn"
              aria-pressed={paused}
              aria-label={pauseName}
              title={playBlocked ? AT_LIVE_EDGE_TITLE : pauseName}
              disabled={playBlocked}
              onClick={onTogglePause}
            >
              <ToolbarIcon>{paused ? PLAY_ICON : PAUSE_ICON}</ToolbarIcon>
              <span className="toolbar-btn-text">{paused ? (archive ? "Play" : "Resume") : "Pause"}</span>
            </button>
            <button
              className="btn btn-secondary toggle-btn"
              aria-label="Fit"
              title="Fit the view to everything on the map"
              onClick={onFit}
            >
              <ToolbarIcon>{FIT_ICON}</ToolbarIcon>
              <span className="toolbar-btn-text">Fit</span>
            </button>
            {/* Carries the filter count the Filters button carries on a wide
                screen, so a filter left on still shows with the sheet shut. */}
            <button
              className="btn btn-secondary toggle-btn toolbar-layers-btn"
              aria-label={layersName}
              aria-haspopup="dialog"
              aria-expanded={layersOpen}
              title={layersName}
              onClick={(e) => {
                // The sheet hands focus back to whatever held it when it
                // opened, and Safari does not focus a tapped button: taken
                // here, it comes back to this button when the sheet closes.
                e.currentTarget.focus();
                openLayers();
              }}
            >
              <ToolbarIcon>{LAYERS_ICON}</ToolbarIcon>
              <span className="toolbar-btn-text">Layers</span>
              {filterCount > 0 && (
                <span className="toolbar-layers-count" aria-hidden="true">
                  {filterCount}
                </span>
              )}
            </button>
            {more}
            {menu}
          </div>
        </div>

        <LayersSheet
          open={layersOpen}
          onClose={closeLayers}
          initialFocus={focusFilters ? firstFilterRef : undefined}
          groups={layers}
          filters={<FilterFields filters={filters} onChange={onFiltersChange} firstFieldRef={firstFilterRef} />}
          onClearFilters={() => onFiltersChange(noFilters())}
        />
      </div>
    );
  }

  return (
    <div className="live-map-toolbar">
      <div className="toolbar-row-status">
        {pill}
        <span className="aircraft-count">{aircraftCount} aircraft</span>

        <div className="toolbar-spacer" />

        <div className="toolbar-actions" ref={actionsRef}>
          <button
            className="btn btn-secondary toggle-btn"
            aria-pressed={paused}
            aria-label={archive ? (paused ? "Play replay" : "Pause replay") : undefined}
            title={playBlocked ? AT_LIVE_EDGE_TITLE : undefined}
            disabled={playBlocked}
            onClick={onTogglePause}
          >
            {paused ? (archive ? "▶ Play" : "▶ Resume") : "⏸ Pause"}
          </button>
          <button className="btn btn-secondary toggle-btn" onClick={onFit} title="Fit the view to everything on the map">
            ◎ Fit
          </button>
          {more}
          {menu}
        </div>
      </div>

      <div className="toolbar-break" />

      <div className="toolbar-row-layers">
        {layers.map((group, i) => (
          <Fragment key={group.label}>
            {i > 0 && <div className="toolbar-separator" />}
            <div className="toolbar-group">
              <span className="toolbar-group-label">{group.label}</span>
              {group.switches.map((s) =>
                s.opensFilters ? (
                  <div key={s.id} className="toolbar-filters-anchor" ref={filtersRef}>
                    <button className="btn btn-secondary toggle-btn" onClick={s.onToggle} aria-expanded={s.on}>
                      {s.label}
                    </button>
                    {s.on && <FiltersPopover filters={filters} onChange={onFiltersChange} anchorRef={filtersRef} />}
                  </div>
                ) : (
                  <button
                    key={s.id}
                    className={s.alert ? "btn btn-secondary toggle-btn alert" : "btn btn-secondary toggle-btn"}
                    aria-pressed={s.on}
                    onClick={s.onToggle}
                    title={s.title}
                  >
                    {s.label}
                  </button>
                ),
              )}
            </div>
          </Fragment>
        ))}
      </div>
    </div>
  );
}

/** The compact bar's icons: line drawings in the button's own colour, sized
 *  to sit where the desktop's glyphs sit. */
function ToolbarIcon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="toolbar-icon"
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

const PLAY_ICON = <path d="M5 3.25v9.5L12.5 8z" fill="currentColor" />;
const PAUSE_ICON = <path d="M5.5 3.5v9M10.5 3.5v9" strokeWidth="2.25" />;
const FIT_ICON = (
  <>
    <circle cx="8" cy="8" r="4.25" />
    <path d="M8 1.25v2.5M8 12.25v2.5M1.25 8h2.5M12.25 8h2.5" />
  </>
);
const LAYERS_ICON = (
  <>
    <path d="M8 1.75 1.75 5 8 8.25 14.25 5z" />
    <path d="M1.75 8 8 11.25 14.25 8" />
    <path d="M1.75 11 8 14.25 14.25 11" />
  </>
);

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

/** None of the four fields set: the whole fleet. */
const noFilters = () => ({ minFl: "", maxFl: "", minGs: "", type: "all" });

const POPOVER_WIDTH = 232;
const VIEWPORT_MARGIN = 8;
const GAP = 6;

function FiltersPopover({ filters, onChange, anchorRef }) {
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
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      ro?.disconnect();
    };
  }, [anchorRef]);

  return (
    <div
      ref={popRef}
      className="card filters-popover"
      // Hidden until measured, so it can never be seen at a placeholder
      // position — the failure mode of placing in an effect.
      style={pos ? { top: pos.top, left: pos.left } : { visibility: "hidden", top: 0, left: 0 }}
    >
      <div className="card-header">
        Filters
        <button
          className="filter-clear"
          onClick={() => onChange(noFilters())}
        >
          Clear all
        </button>
      </div>
      <div className="card-body">
        <FilterFields filters={filters} onChange={onChange} />
      </div>
    </div>
  );
}

/** The four filter fields, for the popover and, inline, the Layers sheet,
 *  which can open with `firstFieldRef`'s field focused. */
function FilterFields({ filters, onChange, firstFieldRef = undefined }) {
  const set = (patch) => onChange((f) => ({ ...f, ...patch }));

  return (
    <>
      <label className="filter-row">
        <span className="filter-label">Flight level</span>
        <span className="filter-inputs">
          <input
            ref={firstFieldRef}
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
    </>
  );
}
