import type { FormEvent, ReactNode } from "react";

import { Notice } from "./Notice";
import type { PolledRadarProbe, RadarSite } from "../types";
import { formatMHz } from "../utils/format";
import { distanceKm } from "../utils/geo";

/* The cards of useRadarCheck's two steps, shared by registering a stock blah2
   radar and moving one to a new address. */

type Busy = "checking" | "confirming" | null;

function site(s: RadarSite): string {
  return `${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)}, ${Math.round(s.altitude_m)} m`;
}

function named(role: string, s: RadarSite): string {
  return s.name ? `${role} (${s.name})` : role;
}

/** The address to check the radar at, with the check's refusal under it. */
export function AddressCard({
  title,
  address,
  busy,
  refusal,
  onChange,
  onCheck,
  children,
}: {
  title: string;
  address: string;
  busy: Busy;
  refusal: string | null;
  onChange: (address: string) => void;
  onCheck: () => void;
  /** Beneath the button. */
  children?: ReactNode;
}) {
  function submit(e: FormEvent) {
    e.preventDefault();
    onCheck();
  }
  return (
    <div className="card">
      <div className="card-header">
        <h3>{title}</h3>
      </div>
      <form className="card-body card-stack" onSubmit={submit} noValidate>
        <label htmlFor="radar-address">Radar address</label>
        <input
          id="radar-address"
          className="input"
          value={address}
          placeholder="radar.example.com:3000"
          autoComplete="off"
          spellCheck={false}
          disabled={busy !== null}
          onChange={(e) => onChange(e.target.value)}
        />
        <p className="card-note">
          host:port, or an http(s) URL. If a password-protected proxy sits in front of it, put the password in the
          address: https://user:password@radar.example.com
        </p>
        {refusal && <Notice>{refusal}</Notice>}
        <div className="btn-row">
          <button type="submit" className="btn btn-primary" disabled={busy !== null || !address.trim()}>
            {busy === "checking" ? "Checking…" : "Check radar"}
          </button>
        </div>
        {busy === "checking" && <p className="card-note">Checking your radar. This can take up to 15 seconds.</p>}
        {children}
      </form>
    </div>
  );
}

/** What the radar declares, read-only: a wrong pin is moved in blah2's own
 *  config and the radar checked again. */
export function DeclarationCard({
  probe,
  busy,
  refusal,
  onCheckAgain,
  onChangeAddress,
}: {
  probe: PolledRadarProbe;
  busy: Busy;
  refusal: string | null;
  onCheckAgain: () => void;
  onChangeAddress: () => void;
}) {
  const baselineKm = distanceKm(probe.rx.latitude, probe.rx.longitude, probe.tx.latitude, probe.tx.longitude);
  return (
    <div className="card">
      <div className="card-header">
        <h3>What your radar declares</h3>
      </div>
      <div className="card-body card-stack">
        <table className="kv-table">
          <tbody>
            <tr>
              <td>Address</td>
              <td className="mono">{probe.address}</td>
            </tr>
            <tr>
              <td>{named("Receiver", probe.rx)}</td>
              <td className="mono">{site(probe.rx)}</td>
            </tr>
            <tr>
              <td>{named("Transmitter", probe.tx)}</td>
              <td className="mono">{site(probe.tx)}</td>
            </tr>
            <tr>
              <td>Frequency</td>
              <td className="mono">{formatMHz(probe.fc_hz)}</td>
            </tr>
            <tr>
              <td>Receiver to transmitter</td>
              <td className="mono">{baselineKm.toFixed(1)} km</td>
            </tr>
          </tbody>
        </table>
        <p className="card-note">Wrong? Move the pin in blah2&rsquo;s own config, then check again.</p>
        {refusal && <Notice>{refusal}</Notice>}
        <div className="btn-row">
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== null} onClick={onCheckAgain}>
            {busy === "checking" ? "Checking…" : "Check again"}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== null} onClick={onChangeAddress}>
            Change address
          </button>
        </div>
      </div>
    </div>
  );
}

/** Standing advice rather than an alert: going ahead anyway is allowed. */
export function UnprotectedNotice({ going }: { going: "register" | "move" }) {
  return (
    <div className="notice">
      <div className="notice-text">
        Anyone who finds this address can read your radar. Put a password-protected proxy in front of it and include
        the password in the address, or {going} it as it is and it will be marked unprotected.
      </div>
    </div>
  );
}
