import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import { PublicationOptions } from "../../components/LocationPrivacy";
import { Notice } from "../../components/Notice";
import { AddressCard, DeclarationCard, UnprotectedNotice } from "../../components/PolledRadar";
import { useAuth } from "../../context/AuthContext";
import { useRadarCheck } from "../../hooks/useRadarCheck";
import type { Publication } from "../../types";

/** Registering a stock 30hours/blah2 radar for the server to poll. The pins are
 *  the radar's own and read-only: a wrong one is moved in blah2's config and
 *  the radar checked again. Registering sends the fingerprint of what the page
 *  showed, and a radar that has changed since comes back to be looked at again. */
export default function RegisterRadarPage() {
  const { polledRadarRegistration } = useAuth();
  const navigate = useNavigate();
  const radar = useRadarCheck((address) => api.probePolledRadar(address));
  const [publication, setPublication] = useState<Publication | null>(null);
  const { probe, busy, refusal } = radar;

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

  async function register() {
    if (!publication) return;
    const registered = await radar.confirm(
      (address, fingerprint) => api.registerPolledRadar({ address, fingerprint, publication }),
      "Could not register the radar. Try again.",
    );
    if (registered) navigate("/onboarding");
  }

  function changeAddress() {
    radar.changeAddress();
    setPublication(null);
  }

  if (!probe) {
    return (
      <>
        {header}
        <AddressCard
          title="Your radar"
          address={radar.address}
          busy={busy}
          refusal={refusal?.message ?? null}
          onChange={radar.setAddress}
          onCheck={radar.check}
        />
      </>
    );
  }

  return (
    <>
      {header}
      <DeclarationCard
        probe={probe}
        busy={busy}
        refusal={refusal?.at === "check" ? refusal.message : null}
        onCheckAgain={radar.check}
        onChangeAddress={changeAddress}
      />
      {!probe.protected && <UnprotectedNotice going="register" />}
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
          {radar.changed && (
            <p className="card-note">The radar&rsquo;s pins have changed: check them above before registering.</p>
          )}
          {refusal?.at === "confirm" && <Notice>{refusal.message}</Notice>}
          <div className="btn-row">
            <button type="button" className="btn btn-primary" disabled={!publication || busy !== null} onClick={register}>
              {busy === "confirming" ? "Registering…" : "Register radar"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
