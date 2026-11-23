import { useId, type ReactNode, type RefObject } from "react";

import { Sheet } from "../../components/Sheet";

/** One switch of the map toolbar's layer row, as Toolbar.tsx builds it from
 *  its props. The row and the sheet both draw from the same list, so a switch
 *  is described once. */
export interface LayerSwitch {
  /** Stays put while the label changes, as it does when a count appears. */
  id: string;
  /** What the switch says, with any count. */
  label: string;
  /** The longer explanation, shown on hover. */
  title?: string;
  on?: boolean;
  onToggle: () => void;
  /** Reports a condition as well as a state, so it is drawn in the error
   *  colour while on. */
  alert?: boolean;
  /** Filters: opens a panel rather than drawing a layer, so the row reports
   *  it as expanded and the sheet shows the panel inline instead. */
  opensFilters?: boolean;
}

export interface LayerGroup {
  label: string;
  switches: LayerSwitch[];
}

interface LayersSheetProps {
  open: boolean;
  onClose: () => void;
  /** Focused on open: a filter field when the sheet opens as the filter
   *  panel. Without it, the first switch. */
  initialFocus?: RefObject<HTMLElement | null>;
  /** The layer row's switches, under its headings. */
  groups: LayerGroup[];
  /** The filter fields, the popover's own. */
  filters: ReactNode;
  onClearFilters: () => void;
}

/**
 * The toolbar's layer row on a phone or tablet: every switch a full-width row
 * a finger can land on, under the row's three headings, and the filters below
 * them, open, since a sheet has the room a popover lacked.
 *
 * It keeps nothing of its own. Each row reads the toolbar's props and calls
 * its callbacks, so a switch flipped here is flipped on the map, and one
 * flipped elsewhere while the sheet is open, by a shortcut say, shows here at
 * once. Flipping one leaves the sheet open, to flip the next.
 */
export default function LayersSheet({
  open,
  onClose,
  initialFocus,
  groups,
  filters,
  onClearFilters,
}: LayersSheetProps) {
  const filtersLabel = groups.flatMap((g) => g.switches).find((s) => s.opensFilters)?.label ?? "Filters";

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="Layers"
      variant="bottom"
      initialFocus={initialFocus}
      className="layers-sheet"
    >
      {groups.map((group) => (
        <SwitchGroup key={group.label} group={group} />
      ))}
      <FiltersGroup label={filtersLabel} onClear={onClearFilters}>
        {filters}
      </FiltersGroup>
    </Sheet>
  );
}

function SwitchGroup({ group }: { group: LayerGroup }) {
  const headingId = useId();
  return (
    <div className="layers-group" role="group" aria-labelledby={headingId}>
      <h3 id={headingId} className="layers-group-title">
        {group.label}
      </h3>
      {group.switches
        .filter((s) => !s.opensFilters)
        .map((s) => (
          <button
            key={s.id}
            type="button"
            role="switch"
            aria-checked={Boolean(s.on)}
            className={s.alert ? "layers-row alert" : "layers-row"}
            title={s.title}
            onClick={s.onToggle}
          >
            <span className="layers-row-label">{s.label}</span>
            <span className="layers-row-switch" aria-hidden="true" />
          </button>
        ))}
    </div>
  );
}

function FiltersGroup({ label, onClear, children }: { label: string; onClear: () => void; children: ReactNode }) {
  const headingId = useId();
  return (
    <div className="layers-group layers-filters" role="group" aria-labelledby={headingId}>
      <div className="layers-filters-head">
        <h3 id={headingId} className="layers-group-title">
          {label}
        </h3>
        <button type="button" className="filter-clear" onClick={onClear}>
          Clear all
        </button>
      </div>
      <div className="layers-filters-fields">{children}</div>
    </div>
  );
}
