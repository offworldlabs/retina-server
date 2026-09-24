import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { HttpError } from "@retina/shared";

import { api } from "../../api/client";
import { PublicationOptions } from "../../components/LocationPrivacy";
import { Notice } from "../../components/Notice";
import { useAuth } from "../../context/AuthContext";
import type { PolledRadarProbe, Publication, RadarSite } from "../../types";
import { formatMHz } from "../../utils/format";
import { distanceKm } from "../../utils/geo";

function site(s: RadarSite): string {
  return `${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)}, ${Math.round(s.altitude_m)} m`;
}

function named(role: string, s: RadarSite): string {
  return s.name ? `${role} (${s.name})` : role;
}

/** Registering a stock 30hours/blah2 radar for the server to poll. The pins are
 *  the radar's own and read-only: a wrong one is moved in blah2's config and
 *  the radar checked again. Registering sends the fingerprint of what the page
 *  showed, and a radar that has changed since comes back to be looked at again. */
export default function RegisterRadarPage() {
  const { polledRadarRegistration } = useAuth();
  const navigate = useNavigate();
  const [address, setAddress] = useState("");
  const [probe, setProbe] = useState<PolledRadarProbe | null>(null);
  const [publication, setPublication] = useState<Publication | null>(null);
  const [busy, setBusy] = useState<"checking" | "registering" | null>(null);
  // Shown beside what it concerns: the address or the pins, or the button.
  const [refusal, setRefusal] = useState<{ at: "check" | "register"; message: string } | null>(null);
  const [changed, setChanged] = useState(false);

  const header = (
    <div className="page-header">
      <h1>Add a stock blah2 radar</h1>
      <p>
        For a radar running 30hours/blah2 whose web API the internet can reach. We check it now, then poll it
        every second.
      </p>
    </div>
  );
  if (!polledRadarRegistration) {
    return (
      <>
        {header}
        <div className="empty-state">Registration of stock blah2 radars is not open yet.</div>
      </>
    );
  }

  async function check(e?: FormEvent) {
    e?.preventDefault();
    setRefusal(null);
    setChanged(false);
    setBusy("checking");
    try {
      setProbe(await api.probePolledRadar(address.trim()));
    } catch (err) {
      setRefusal({ at: "check", message: (err as Error).message || "Could not check the radar. Try again." });
    } finally {
      setBusy(null);
    }
  }

  async function register() {
    if (!probe || !publication) return;
    setRefusal(null);
    setChanged(false);
    setBusy("registering");
    try {
      await api.registerPolledRadar({ address: address.trim(), fingerprint: probe.fingerprint, publication });
      navigate("/onboarding");
      return;
    } catch (err) {
      const body = err instanceof HttpError ? (err.body as { code?: string; probe?: PolledRadarProbe } | null) : null;
      const message = (err as Error).message || "Could not register the radar. Try again.";
      if (body?.code === "config_changed" && body.probe) {
        // What the radar declares now, for the owner to confirm in its place.
        setProbe(body.probe);
        setChanged(true);
        setRefusal({ at: "check", message });
      } else {
        setRefusal({ at: "register", message });
      }
    }
    setBusy(null);
  }

  function changeAddress() {
    setProbe(null);
    setPublication(null);
    setRefusal(null);
    setChanged(false);
  }

  if (!probe) {
    return (
      <>
        {header}
        <div className="card">
          <div className="card-header">
            <h3>Your radar</h3>
          </div>
          <form className="card-body card-stack" onSubmit={check} noValidate>
            <label htmlFor="radar-address">Radar address</label>
            <input
              id="radar-address"
              className="input"
              value={address}
              placeholder="radar.example.com:3000"
              autoComplete="off"
              spellCheck={false}
              disabled={busy !== null}
              onChange={(e) => setAddress(e.target.value)}
            />
            <p className="card-note">
              host:port, or an http(s) URL. If a password-protected proxy sits in front of it, put the password in
              the address: https://user:password@radar.example.com
            </p>
            {refusal && <Notice>{refusal.message}</Notice>}
            <div className="btn-row">
              <button type="submit" className="btn btn-primary" disabled={busy !== null || !address.trim()}>
                {busy === "checking" ? "Checking…" : "Check radar"}
              </button>
            </div>
            {busy === "checking" && <p className="card-note">Checking your radar. This can take up to 15 seconds.</p>}
          </form>
        </div>
      </>
    );
  }

  const baselineKm = distanceKm(probe.rx.latitude, probe.rx.longitude, probe.tx.latitude, probe.tx.longitude);
  return (
    <>
      {header}
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
          {refusal?.at === "check" && <Notice>{refusal.message}</Notice>}
          <div className="btn-row">
            <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== null} onClick={() => check()}>
              {busy === "checking" ? "Checking…" : "Check again"}
            </button>
            <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== null} onClick={changeAddress}>
              Change address
            </button>
          </div>
        </div>
      </div>
      {!probe.protected && (
        // Standing advice rather than an alert: registering it anyway is allowed.
        <div className="notice">
          <div className="notice-text">
            Anyone who finds this address can read your radar. Put a password-protected proxy in front of it and
            include the password in the address, or register it as it is and it will be marked unprotected.
          </div>
        </div>
      )}
      <div className="card">
        <div className="card-header">
          <h3>Publication</h3>
        </div>
        <div className="card-body card-stack">
          <PublicationOptions
            name="publication"
            value={publication}
            disabled={busy !== null}
            onChange={setPublication}
          />
          <p className="card-note">
            It starts on probation: it is on your nodes list and map at once, and stays out of the network&rsquo;s solves,
            the archive and the public map until an administrator has reviewed it.
          </p>
          {changed && <p className="card-note">The radar&rsquo;s pins have changed: check them above before registering.</p>}
          {refusal?.at === "register" && <Notice>{refusal.message}</Notice>}
          <div className="btn-row">
            <button type="button" className="btn btn-primary" disabled={!publication || busy !== null} onClick={register}>
              {busy === "registering" ? "Registering…" : "Register radar"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
