import { useState } from "react";
import type { LocationPrivacySource, LocationPrivacyState } from "../types";

/** The two answers, in the words the dashboard is contracted to use.
 *  `services/publication.py` is what actually enforces them; keep this copy in
 *  step with that module's redaction rules rather than paraphrasing it. */
export const PRIVATE_DESCRIPTION =
  "Hidden from the public map: no marker, no coverage, no detection arcs, " +
  "no single-node aircraft. Still contributes to multi-node solves.";

export function publicDescription(uncertaintyKm?: number | null): string {
  // The number comes from the node's own detection area when the server has
  // one; without it the deployment's fuzz is still applied, it just cannot be
  // quoted, so the sentence names the mechanism instead of a bogus distance.
  const displacement =
    typeof uncertaintyKm === "number" && Number.isFinite(uncertaintyKm) && uncertaintyKm > 0
      ? `up to ${formatKm(uncertaintyKm)} km`
      : "by the network's location fuzz";
  return `Shown on the map at an approximate position, displaced ${displacement}`;
}

function formatKm(km: number): string {
  return km >= 10 ? String(Math.round(km)) : String(Math.round(km * 10) / 10);
}

/** Compact marker for a private node in any list of the user's own nodes.
 *  Renders nothing for a public node, so callers can drop it in unguarded. */
export function LocationPrivacyBadge({ isPrivate }: { isPrivate?: boolean | null }) {
  if (!isPrivate) return null;
  return (
    <span className="badge private" title={PRIVATE_DESCRIPTION}>
      Private
    </span>
  );
}

interface Props {
  /** Only used to keep the radio group distinct when several are on a page. */
  nodeId: string;
  /** Effective state as the server last reported it. */
  isPrivate: boolean;
  source: LocationPrivacySource;
  /** `detection_area.rx.location_uncertainty_km`, when the caller has it. */
  uncertaintyKm?: number | null;
  /** Unix seconds an override was set; only the admin route reports it. */
  setAt?: number | null;
  onSave: (isPrivate: boolean) => Promise<LocationPrivacyState>;
  onReset: () => Promise<LocationPrivacyState>;
  /** Told about every state the server confirms, so a parent list can keep
   *  its badges honest without refetching. */
  onApplied?: (state: LocationPrivacyState) => void;
  /** Drops the per-option prose (kept as a tooltip) for dense lists. */
  compact?: boolean;
}

/** Two-option location privacy control, shared by the owner's node page and
 *  the admin node list; the caller supplies the routes through onSave/onReset
 *  so the same control drives both the /me and /admin endpoints.
 *
 *  Updates optimistically and rolls back to the props on failure: the write is
 *  a single boolean, so showing the new answer immediately and undoing it on
 *  error is less confusing than a spinner on every click. */
export function LocationPrivacyControl({
  nodeId,
  isPrivate,
  source,
  uncertaintyKm,
  setAt,
  onSave,
  onReset,
  onApplied,
  compact = false,
}: Props) {
  // null means "nothing local to say" — the props are the truth. A rollback is
  // therefore just dropping back to null rather than remembering a prior value.
  const [local, setLocal] = useState<LocationPrivacyState | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const shownPrivate = local ? local.location_private : isPrivate;
  const shownSource = local ? local.location_privacy_source : source;
  const shownSetAt = local ? (local.set_at ?? null) : (setAt ?? null);

  const run = async (
    optimistic: LocationPrivacyState,
    call: () => Promise<LocationPrivacyState>,
  ) => {
    setLocal(optimistic);
    setSaving(true);
    setError(null);
    try {
      const confirmed = await call();
      setLocal(confirmed);
      onApplied?.(confirmed);
    } catch (e: any) {
      setLocal(null); // rollback to whatever the server last told the parent
      setError(e?.message || "Could not save. Try again.");
    } finally {
      setSaving(false);
    }
  };

  const choose = (next: boolean) => {
    if (saving || next === shownPrivate) return;
    void run(
      { node_id: nodeId, location_private: next, location_privacy_source: "override" },
      () => onSave(next),
    );
  };

  const reset = () => {
    if (saving) return;
    // The effective state after a reset is the registration choice, which the
    // caller does not necessarily know, so the optimistic frame keeps the
    // current answer and only the source line goes quiet until the server
    // answers. Nothing here is a guess that can be wrong on screen.
    void run(
      { node_id: nodeId, location_private: shownPrivate, location_privacy_source: shownSource },
      () => onReset(),
    );
  };

  const name = `location-privacy-${nodeId}`;
  const publicDesc = publicDescription(uncertaintyKm);

  return (
    <div className="privacy-control">
      <div className="privacy-options">
        <label className="privacy-option" title={compact ? publicDesc : undefined}>
          <input
            type="radio"
            name={name}
            value="public"
            checked={!shownPrivate}
            disabled={saving}
            onChange={() => choose(false)}
          />
          <span>
            <span className="privacy-option-title">Public</span>
            {!compact && <span className="privacy-option-desc">{publicDesc}</span>}
          </span>
        </label>
        <label className="privacy-option" title={compact ? PRIVATE_DESCRIPTION : undefined}>
          <input
            type="radio"
            name={name}
            value="private"
            checked={shownPrivate}
            disabled={saving}
            onChange={() => choose(true)}
          />
          <span>
            <span className="privacy-option-title">Private</span>
            {!compact && <span className="privacy-option-desc">{PRIVATE_DESCRIPTION}</span>}
          </span>
        </label>
      </div>

      <div className="privacy-source">
        <span>{sourceLine(shownSource, shownSetAt)}</span>
        {shownSource === "override" && (
          <button type="button" className="link-button" disabled={saving} onClick={reset}>
            Use the onboarding choice
          </button>
        )}
        {saving && <span className="privacy-saving">Saving…</span>}
      </div>

      {error && (
        <div className="privacy-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

function sourceLine(source: LocationPrivacySource, setAt: number | null): string {
  if (source === "registration") return "Set at onboarding";
  if (source === "override") {
    // The owner routes report no timestamp, only the admin one does, so the
    // date is stated when it is known and omitted rather than invented.
    return setAt ? `Set here on ${new Date(setAt * 1000).toLocaleDateString()}` : "Set here";
  }
  return "Not set — public by default";
}
