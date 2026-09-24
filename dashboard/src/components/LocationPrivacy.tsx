import type { Publication } from "../types";

/** The two answers, in the words the dashboard is contracted to use.
 *  `services/publication.py` is what actually enforces them; keep this copy in
 *  step with that module's redaction rules rather than paraphrasing it. */
export const PRIVATE_DESCRIPTION =
  "Hidden from the public map: no marker, no coverage, no detection arcs, " +
  "no single-node aircraft. Still contributes to multi-node solves.";

const PUBLIC_DESCRIPTION =
  "Shown on the map at an approximate position, displaced by the network's location fuzz";

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

interface PublicationOptionsProps {
  /** The radio group's name, distinct for each group on a page. */
  name: string;
  /** null while nothing is chosen. */
  value: Publication | null;
  disabled?: boolean;
  onChange: (choice: Publication) => void;
}

/** The two answers as a radio group, in the contracted words. */
export function PublicationOptions({
  name,
  value,
  disabled = false,
  onChange,
}: PublicationOptionsProps) {
  return (
    <div className="privacy-options">
      <label className="privacy-option">
        <input
          type="radio"
          name={name}
          value="public"
          checked={value === "public"}
          disabled={disabled}
          onChange={() => onChange("public")}
        />
        <span>
          <span className="privacy-option-title">Public</span>
          <span className="privacy-option-desc">{PUBLIC_DESCRIPTION}</span>
        </span>
      </label>
      <label className="privacy-option">
        <input
          type="radio"
          name={name}
          value="private"
          checked={value === "private"}
          disabled={disabled}
          onChange={() => onChange("private")}
        />
        <span>
          <span className="privacy-option-title">Private</span>
          <span className="privacy-option-desc">{PRIVATE_DESCRIPTION}</span>
        </span>
      </label>
    </div>
  );
}
