import { YoursBadge } from "../../../components/YoursBadge";
import type { NameOf } from "./nodes";

/** How many nodes a cell names before it counts the rest. A list one longer
 *  is shown whole: "+1 more" would take the room the name needs. */
const SHOWN = 2;

interface SeenByProps {
  /** The nodes behind the aircraft, as nodesBehind gives them. */
  refs: readonly string[];
  nameOf: NameOf;
  /** The signed-in owner's node_refs. */
  mine: ReadonlySet<string>;
}

/**
 * The "Seen by" cell: the nodes behind one aircraft, the owner's first and
 * marked as theirs. A long list is cut to the first few and a count. The rest
 * are in the count's tooltip, and a screen reader is read them in its place.
 */
export function SeenBy({ refs, nameOf, mine }: SeenByProps) {
  if (refs.length === 0) return <>—</>;

  const ordered = [...refs.filter((ref) => mine.has(ref)), ...refs.filter((ref) => !mine.has(ref))];
  const shown = ordered.length > SHOWN + 1 ? ordered.slice(0, SHOWN) : ordered;
  const rest = ordered
    .slice(shown.length)
    .map((ref) => `${nameOf(ref).text}${mine.has(ref) ? " (yours)" : ""}`)
    .join(", ");
  const hidden = ordered.length - shown.length;

  return (
    <ul className="seen-by">
      {shown.map((ref) => {
        const label = nameOf(ref);
        return (
          <li key={ref} className="seen-by-node">
            <span className={label.isRef ? "seen-by-name mono" : "seen-by-name"} title={label.text}>
              {label.text}
            </span>
            {mine.has(ref) && <YoursBadge />}
          </li>
        );
      })}
      {hidden > 0 && (
        <li className="seen-by-more">
          <span aria-hidden="true" title={rest}>
            +{hidden} more
          </span>
          <span className="seen-by-hidden">
            and {hidden} more: {rest}
          </span>
        </li>
      )}
    </ul>
  );
}
