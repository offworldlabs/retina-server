import { useId, type ReactNode } from "react";

import type { Scope } from "../hooks/useScopeParam";
import "./ownerScope.css";

interface ScopeSwitchProps {
  value: Scope;
  onChange: (next: Scope) => void;
  /** The group's name for assistive tech; not drawn. */
  label?: string;
  mineLabel?: string;
  networkLabel?: string;
  /** How many nodes "mine" covers, drawn as "My nodes · 3". */
  mineCount?: number;
  disabled?: boolean;
}

interface SegmentProps {
  name: string;
  scope: Scope;
  checked: boolean;
  onChange: (next: Scope) => void;
  children: ReactNode;
}

/** The radio lies over the whole segment, so a tap anywhere on it lands on
 *  the input itself, and a touch screen reader finds it where it is drawn. */
function Segment({ name, scope, checked, onChange, children }: SegmentProps) {
  return (
    <label className="scope-switch-option">
      <input type="radio" name={name} value={scope} checked={checked} onChange={() => onChange(scope)} />
      <span className="scope-switch-text">{children}</span>
    </label>
  );
}

/**
 * "My nodes / Whole network". Native radios in a fieldset, so the browser
 * gives the group one tab stop at the checked option and moves it with the
 * arrow keys, and a disabled fieldset disables both.
 */
export function ScopeSwitch({
  value,
  onChange,
  label = "Show data from",
  mineLabel = "My nodes",
  networkLabel = "Whole network",
  mineCount,
  disabled = false,
}: ScopeSwitchProps) {
  // One group per switch, should a page ever draw two.
  const name = useId();
  return (
    <fieldset className="scope-switch" disabled={disabled}>
      <legend className="scope-switch-legend">{label}</legend>
      <div className="scope-switch-track">
        <Segment name={name} scope="mine" checked={value === "mine"} onChange={onChange}>
          {mineLabel}
          {mineCount !== undefined && (
            <>
              {" "}
              {/* Drawn but not read: the radio is named "My nodes 3". */}
              <span aria-hidden="true">·</span>{" "}
              <span className="scope-switch-count">{mineCount}</span>
            </>
          )}
        </Segment>
        <Segment name={name} scope="network" checked={value === "network"} onChange={onChange}>
          {networkLabel}
        </Segment>
      </div>
    </fieldset>
  );
}
