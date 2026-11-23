import { useMemo, useState, type ComponentProps } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Toolbar from "./Toolbar";
import { AT_LIVE_EDGE_TITLE } from "./replay/useArchiveReplay";
import { useKeyboardShortcuts } from "./useKeyboardShortcuts";
import { stubViewport } from "../../test/matchMedia";

type ToolbarProps = ComponentProps<typeof Toolbar>;

// Below desktop width the toolbar is one row and its layer switches sit in a
// sheet that Layers opens. These cases hold the switches' state the way the
// map does, above the toolbar, and pass it down, so a switch is proved to be
// one state shown in two places.

const SWITCHES = {
  showCoverage: false,
  showArcs: true,
  showIlluminators: false,
  showTrails: false,
  showLabels: false,
  showGroundTruth: false,
  showAnomaliesOnly: false,
  showInBeamDiag: false,
  showUncertainty: false,
  showRangeRings: false,
  colorByAlt: false,
  followSelected: false,
  showFilters: false,
  showStats: true,
};
type Switch = keyof typeof SWITCHES;

/** The map's side of the toolbar: real state for every switch and the
 *  filters, flipped by the toolbar's callbacks and by the map's `t` and `f`
 *  shortcuts, `f` wired as the map wires it, straight to the state. */
function WithMapState({
  onFiltersToggled = () => {},
  initial = {},
}: {
  onFiltersToggled?: () => void;
  initial?: Partial<typeof SWITCHES>;
}) {
  const [state, setState] = useState({ ...SWITCHES, ...initial });
  const [filters, setFilters] = useState({ minFl: "", maxFl: "", minGs: "", type: "all" });
  const flip = (key: Switch) => () => setState((s) => ({ ...s, [key]: !s[key] }));
  useKeyboardShortcuts(
    useMemo(
      () => ({
        t: () => setState((s) => ({ ...s, showTrails: !s.showTrails })),
        f: () => setState((s) => ({ ...s, showFilters: !s.showFilters })),
      }),
      [],
    ),
  );
  return (
    <div className="map-surface">
      <Toolbar
        {...({
          connected: true,
          paused: false,
          aircraftCount: 41,
          anomalyCount: 0,
          truthAvailable: true,
          ...state,
          filters,
          onFiltersChange: setFilters,
          onToggleCoverage: flip("showCoverage"),
          onToggleArcs: flip("showArcs"),
          onToggleIlluminators: flip("showIlluminators"),
          onToggleTrails: flip("showTrails"),
          onToggleLabels: flip("showLabels"),
          onToggleGroundTruth: flip("showGroundTruth"),
          onToggleAnomaliesOnly: flip("showAnomaliesOnly"),
          onToggleInBeamDiag: flip("showInBeamDiag"),
          onToggleUncertainty: flip("showUncertainty"),
          onToggleRangeRings: flip("showRangeRings"),
          onToggleColorByAlt: flip("colorByAlt"),
          onToggleFollow: flip("followSelected"),
          onToggleFilters: () => {
            onFiltersToggled();
            flip("showFilters")();
          },
          onToggleStats: flip("showStats"),
          onTogglePause: () => {},
          onFit: () => {},
        } as ToolbarProps)}
      />
    </div>
  );
}

const HASH = "#lat=34.85&lon=-82.39&z=9";
const DESKTOP_ROW = [
  "Coverage",
  "Arcs",
  "Illuminators",
  "Trails",
  "Labels",
  "Debug Truth",
  "⚠ Anomalies",
  "Coverage gaps",
  "σ Uncert.",
  "Range rings",
  "Alt colour",
  "Follow",
  "Filters",
  "Stats",
];
const SHEET_ROWS = DESKTOP_ROW.filter((name) => name !== "Filters");

const bar = () => document.querySelector(".live-map-toolbar") as HTMLElement;
const layersButton = () => screen.getByRole("button", { name: /^Layers/ });
const sheet = () => screen.getByRole("dialog", { name: "Layers" });
const row = (name: string) => within(sheet()).getByRole("switch", { name });
const desktopButton = (name: string) =>
  within(bar().querySelector(".toolbar-row-layers") as HTMLElement).getByRole("button", { name });

beforeEach(() => window.history.replaceState(null, "", `/map${HASH}`));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the map toolbar on a phone", () => {
  it("is one row: the feed, the count, play/pause, fit, Layers and the menu, with no layer switch", () => {
    stubViewport(390);
    render(<WithMapState />);
    expect([...bar().children].map((el) => el.className)).toEqual(["toolbar-row-status"]);
    const status = bar().firstElementChild as HTMLElement;
    expect(status.querySelector(".connection-badge")).toHaveTextContent(/^LIVE$/);
    expect(status.querySelector(".aircraft-count")).toHaveTextContent(/^41 aircraft$/);
    expect(within(status).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.title)).toEqual([
      "Pause",
      "Fit",
      "Layers",
      "More actions",
    ]);
    for (const name of DESKTOP_ROW) expect(screen.queryByRole("button", { name })).toBeNull();
  });

  it("opens a sheet holding every switch of the desktop row, under its headings, and the filters", () => {
    stubViewport(390);
    render(<WithMapState />);
    fireEvent.click(layersButton());
    expect(layersButton()).toHaveAttribute("aria-expanded", "true");
    expect(within(sheet()).getAllByRole("switch").map((el) => el.textContent)).toEqual(SHEET_ROWS);
    expect(within(sheet()).getAllByRole("group").map((el) => el.querySelector("h3")?.textContent)).toEqual([
      "Overlays",
      "Analysis",
      "View",
      "Filters",
    ]);
    expect(row("Arcs")).toHaveAttribute("aria-checked", "true");
    expect(row("Trails")).toHaveAttribute("aria-checked", "false");
    const filters = within(sheet()).getByRole("group", { name: "Filters" });
    expect(within(filters).getByPlaceholderText("min")).toBeInTheDocument();
    expect(within(filters).getByPlaceholderText("max")).toBeInTheDocument();
    expect(within(filters).getByPlaceholderText("0")).toBeInTheDocument();
    expect(within(filters).getByRole("combobox")).toHaveValue("all");
    expect(within(filters).getByRole("button", { name: "Clear all" })).toBeInTheDocument();
  });

  it("carries what the sheet changes to the desktop row, and the desktop row's changes back, never touching the hash", () => {
    const viewport = stubViewport(390);
    render(<WithMapState />);
    fireEvent.click(layersButton());
    fireEvent.click(row("Trails"));
    expect(row("Trails")).toHaveAttribute("aria-checked", "true");
    fireEvent.change(within(sheet()).getByPlaceholderText("min"), { target: { value: "120" } });
    expect(sheet()).toBeInTheDocument();
    expect(within(sheet()).getByRole("group", { name: "Filters (1)" })).toBeInTheDocument();
    expect(layersButton()).toHaveAccessibleName("Layers, 1 filter on");

    viewport.resize(1440);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Layers/ })).toBeNull();
    expect(desktopButton("Trails")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(desktopButton("Filters (1)"));
    expect(document.querySelector(".filters-popover input[placeholder='min']")).toHaveValue(120);
    fireEvent.click(desktopButton("Filters (1)"));
    fireEvent.click(desktopButton("Labels"));
    expect(desktopButton("Labels")).toHaveAttribute("aria-pressed", "true");

    viewport.resize(390);
    // Shut as the window widened, and not back by itself.
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(layersButton());
    expect(row("Labels")).toHaveAttribute("aria-checked", "true");
    expect(row("Trails")).toHaveAttribute("aria-checked", "true");
    expect(within(sheet()).getByPlaceholderText("min")).toHaveValue(120);
    expect(window.location.hash).toBe(HASH);
  });

  it("names the menu's button by what it holds, not its glyph", () => {
    stubViewport(390);
    render(<WithMapState />);
    expect(screen.getByRole("button", { name: "More actions" })).toHaveAttribute("aria-haspopup", "menu");
  });

  it("shows a switch flipped while the sheet is open, by a shortcut say", () => {
    stubViewport(390);
    render(<WithMapState />);
    fireEvent.click(layersButton());
    expect(row("Trails")).toHaveAttribute("aria-checked", "false");
    fireEvent.keyDown(window, { key: "t" });
    expect(row("Trails")).toHaveAttribute("aria-checked", "true");
    expect(sheet()).toBeInTheDocument();
  });

  it("shuts the menu as the sheet opens", () => {
    stubViewport(390);
    render(<WithMapState />);
    fireEvent.click(screen.getByTitle("More actions"));
    expect(screen.getAllByRole("menuitem").map((el) => el.textContent)).toEqual([
      "Centre on my location m",
      "Copy link to this view",
      "Export trails as CSV ⇧X",
      "Emergency squawk alertoff",
      "Basemap OSM",
      "Keyboard shortcuts ?",
    ]);
    fireEvent.click(layersButton());
    expect(screen.queryByRole("menu")).toBeNull();
    expect(sheet()).toBeInTheDocument();
  });

  it("hands focus back to Layers when Escape shuts the sheet, though the press did not focus the button", () => {
    stubViewport(390);
    render(<WithMapState />);
    expect(document.body).toHaveFocus();
    fireEvent.click(layersButton());
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(layersButton()).toHaveFocus();
  });

  it("hands focus back to Layers when Close shuts the sheet, though the press did not focus the button", () => {
    stubViewport(390);
    render(<WithMapState />);
    expect(document.body).toHaveFocus();
    fireEvent.click(layersButton());
    fireEvent.click(within(sheet()).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(layersButton()).toHaveFocus();
  });

  it("keeps the archive's Play, held at the live edge, and its Pause", () => {
    stubViewport(360);
    const props = { connected: true, aircraftCount: 0, filters: {}, archive: true, onTogglePause: vi.fn() };
    const { rerender } = render(<Toolbar {...({ ...props, paused: true, atLiveEdge: true } as ToolbarProps)} />);
    expect(bar()).toHaveClass("toolbar-compact");
    expect(bar().querySelector(".toolbar-row-layers")).toBeNull();
    const play = screen.getByRole("button", { name: "Play replay" });
    expect(play.querySelector(".toolbar-btn-text")).toHaveTextContent(/^Play$/);
    expect(play).toBeDisabled();
    expect(play).toHaveAttribute("title", AT_LIVE_EDGE_TITLE);
    expect(document.querySelector(".connection-badge")).toHaveTextContent(/^ARCHIVE$/);
    rerender(<Toolbar {...({ ...props, paused: false, atLiveEdge: true } as ToolbarProps)} />);
    const pause = screen.getByRole("button", { name: "Pause replay" });
    expect(pause).toBeEnabled();
    fireEvent.click(pause);
    expect(props.onTogglePause).toHaveBeenCalledTimes(1);
  });
});

describe("the filter panel below desktop width", () => {
  it("is the sheet, opened at the first filter field by the map's f, and shut with the sheet by Close or Escape", () => {
    const viewport = stubViewport(820);
    const onFiltersToggled = vi.fn();
    render(<WithMapState onFiltersToggled={onFiltersToggled} />);

    fireEvent.keyDown(window, { key: "f" });
    const filters = within(sheet()).getByRole("group", { name: "Filters" });
    expect(within(filters).getByPlaceholderText("min")).toHaveFocus();
    fireEvent.click(within(sheet()).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onFiltersToggled).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: "f" });
    expect(within(sheet()).getByPlaceholderText("min")).toHaveFocus();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onFiltersToggled).toHaveBeenCalledTimes(2);

    // Nothing left open out of sight for a wider window to show.
    viewport.resize(1440);
    expect(document.querySelector(".filters-popover")).toBeNull();
    expect(desktopButton("Filters")).toHaveAttribute("aria-expanded", "false");
  });

  it("is the sheet, at its first filter field, when the window narrows with the popover open, and a press in it shuts or flips nothing", () => {
    // The panel stays open as the bar changes form. Drawing nothing here
    // would leave it open out of sight, and the map's next f would only shut
    // it, a press that showed nothing.
    const viewport = stubViewport(1440);
    const onFiltersToggled = vi.fn();
    render(<WithMapState onFiltersToggled={onFiltersToggled} />);
    fireEvent.click(desktopButton("Filters"));
    expect(document.querySelector(".filters-popover")).not.toBeNull();
    onFiltersToggled.mockClear();

    viewport.resize(820);
    expect(document.querySelector(".filters-popover")).toBeNull();
    expect(within(sheet()).getByPlaceholderText("min")).toHaveFocus();
    for (const target of [row("Trails"), within(sheet()).getByPlaceholderText("min"), sheet()]) {
      fireEvent.pointerDown(target);
    }
    expect(onFiltersToggled).not.toHaveBeenCalled();
    expect(sheet()).toBeInTheDocument();
    expect(row("Trails")).toHaveAttribute("aria-checked", "false");

    // Still open, so it is the popover again at desktop width.
    viewport.resize(1440);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.querySelector(".filters-popover")).not.toBeNull();
  });

  it("is the sheet, at its first filter field, when the bar mounts at this width with the panel open", () => {
    stubViewport(820);
    const onFiltersToggled = vi.fn();
    render(<WithMapState onFiltersToggled={onFiltersToggled} initial={{ showFilters: true }} />);
    expect(within(sheet()).getByPlaceholderText("min")).toHaveFocus();
    expect(document.querySelector(".filters-popover")).toBeNull();
    expect(onFiltersToggled).not.toHaveBeenCalled();
  });

  it("is left shut when a sheet that Layers opened is shut, by Close or Escape", () => {
    stubViewport(820);
    const onFiltersToggled = vi.fn();
    render(<WithMapState onFiltersToggled={onFiltersToggled} />);

    fireEvent.click(layersButton());
    fireEvent.click(within(sheet()).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(layersButton());
    expect(row("Coverage")).toHaveFocus();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onFiltersToggled).not.toHaveBeenCalled();
  });

  it("shuts the menu as it opens", () => {
    stubViewport(820);
    render(<WithMapState />);
    fireEvent.click(screen.getByTitle("More actions"));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "f" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(within(sheet()).getByPlaceholderText("min")).toHaveFocus();
  });
});

describe("the map toolbar at desktop width", () => {
  it("has no Layers button and draws the layer row", () => {
    stubViewport(1440);
    render(<WithMapState />);
    expect(screen.queryByRole("button", { name: /^Layers/ })).toBeNull();
    const layers = bar().querySelector(".toolbar-row-layers") as HTMLElement;
    expect(within(layers).getAllByRole("button").map((el) => el.textContent)).toEqual(DESKTOP_ROW);
    expect(screen.getByRole("button", { name: "More actions" })).toHaveAttribute("aria-haspopup", "menu");
  });
});
