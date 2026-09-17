import { addDays } from "./dates";
import { defaultFilters, type ExplorerFilters, TOD_END, TOD_START } from "./urlState";

/** A chip sets a range ending today, so "24h" is today rather than a rolling
 *  day: the archive is partitioned by day and cannot answer finer. */
export const QUICK_RANGES = [
  { days: 1, label: "24h" },
  { days: 3, label: "3d" },
  { days: 7, label: "7d" },
  { days: 14, label: "14d" },
];

const SIZE_STEPS = [
  { bytes: 0, label: "Any size" },
  { bytes: 64 * 1024, label: "≥ 64 KB" },
  { bytes: 1024 * 1024, label: "≥ 1 MB" },
  { bytes: 10 * 1024 * 1024, label: "≥ 10 MB" },
];

interface Props {
  filters: ExplorerFilters;
  today: string;
  onChange: (next: ExplorerFilters) => void;
}

export function DateRangeControls({ filters, today, onChange }: Props) {
  const set = (over: Partial<ExplorerFilters>) => onChange({ ...filters, ...over });
  const quickDays = QUICK_RANGES.find(
    (q) => filters.to === today && filters.from === addDays(today, -(q.days - 1)),
  );

  return (
    <div className="de-filters">
      <div className="de-fgroup">
        <label htmlFor="de-from">From</label>
        <input
          id="de-from"
          type="date"
          value={filters.from}
          max={today}
          onChange={(e) => set({ from: e.target.value })}
        />
        <label htmlFor="de-to">To</label>
        <input
          id="de-to"
          type="date"
          value={filters.to}
          max={today}
          onChange={(e) => set({ to: e.target.value })}
        />
      </div>

      <div className="de-fgroup de-chipset">
        {QUICK_RANGES.map((q) => (
          <button
            key={q.label}
            type="button"
            className={`de-chip${quickDays === q ? " on" : ""}`}
            aria-pressed={quickDays === q}
            onClick={() => set({ from: addDays(today, -(q.days - 1)), to: today })}
          >
            {q.label}
          </button>
        ))}
      </div>

      <div className="de-fgroup">
        <label htmlFor="de-tod-from">From time</label>
        <input
          id="de-tod-from"
          type="time"
          value={filters.todFrom}
          onChange={(e) => set({ todFrom: e.target.value || TOD_START })}
        />
        <label htmlFor="de-tod-to">To time</label>
        <input
          id="de-tod-to"
          type="time"
          value={filters.todTo}
          onChange={(e) => set({ todTo: e.target.value || TOD_END })}
        />
      </div>

      <div className="de-fgroup">
        <label htmlFor="de-minsize">Minimum size</label>
        <select
          id="de-minsize"
          value={String(filters.minSize)}
          onChange={(e) => set({ minSize: Number(e.target.value) })}
        >
          {SIZE_STEPS.map((s) => (
            <option key={s.bytes} value={s.bytes}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      <div className="de-fgroup">
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => onChange(defaultFilters(today))}>
          Reset
        </button>
      </div>
    </div>
  );
}
