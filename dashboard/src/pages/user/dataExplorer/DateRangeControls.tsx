import { formatBytes } from "../../../utils/format";
import type { ExplorerFilters } from "./urlState";
import { addDays } from "./dates";
import { TOD_END, TOD_START } from "./urlState";

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

/** The steps, plus the size in force when a link names one they lack. A select
 *  with no matching option shows its first, "Any size", over a filter that is
 *  still applied. */
function sizeOptions(minSize: number) {
  if (SIZE_STEPS.some((s) => s.bytes === minSize)) return SIZE_STEPS;
  return [...SIZE_STEPS, { bytes: minSize, label: `≥ ${formatBytes(minSize)}` }].sort(
    (a, b) => a.bytes - b.bytes,
  );
}

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
    <>
      <div className="de-fgroup">
        <label htmlFor="de-from">Date range (UTC)</label>
        <div className="de-frow">
          <input
            id="de-from"
            type="date"
            aria-label="From"
            value={filters.from}
            max={today}
            onChange={(e) => set({ from: e.target.value })}
          />
          <span className="de-muted" aria-hidden="true">→</span>
          <input
            id="de-to"
            type="date"
            aria-label="To"
            value={filters.to}
            max={today}
            onChange={(e) => set({ to: e.target.value })}
          />
        </div>
      </div>

      <div className="de-fgroup">
        <label id="de-quick-label">Quick</label>
        <div className="de-chipset" role="group" aria-labelledby="de-quick-label">
          {QUICK_RANGES.map((q) => (
            <button
              key={q.label}
              type="button"
              className="btn btn-outline de-chip"
              aria-pressed={quickDays === q}
              onClick={() => set({ from: addDays(today, -(q.days - 1)), to: today })}
            >
              {q.label}
            </button>
          ))}
        </div>
      </div>

      <div className="de-fgroup">
        <label htmlFor="de-tod-from">Time of day (UTC)</label>
        <div className="de-frow">
          <input
            id="de-tod-from"
            type="time"
            aria-label="From time"
            value={filters.todFrom}
            onChange={(e) => set({ todFrom: e.target.value || TOD_START })}
          />
          <span className="de-muted" aria-hidden="true">→</span>
          <input
            id="de-tod-to"
            type="time"
            aria-label="To time"
            value={filters.todTo}
            onChange={(e) => set({ todTo: e.target.value || TOD_END })}
          />
        </div>
      </div>

      <div className="de-fgroup">
        <label htmlFor="de-minsize">Min size</label>
        <select
          id="de-minsize"
          value={String(filters.minSize)}
          onChange={(e) => set({ minSize: Number(e.target.value) })}
        >
          {sizeOptions(filters.minSize).map((s) => (
            <option key={s.bytes} value={s.bytes}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}
