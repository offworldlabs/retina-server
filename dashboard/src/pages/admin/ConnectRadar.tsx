import { useState } from "react";

import { api } from "../../api/client";
import { PublicationOptions } from "../../components/LocationPrivacy";
import { Notice } from "../../components/Notice";
import { AddressCard, DeclarationCard } from "../../components/PolledRadar";
import { useRadarCheck } from "../../hooks/useRadarCheck";
import type { ConnectionCheck, Publication } from "../../types";

// Enough to hold back a check the server would refuse unread.
const LOOKS_LIKE_AN_ADDRESS = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Connecting a stock blah2 radar to an operator's email address, which need
 *  not have an account yet: one is made for it, and the radar runs on it at
 *  once. What the address has is shown before anything is made, for the
 *  administrator to check by eye: a mistyped one hands the radar and its
 *  location to whoever holds that mailbox. */
export function ConnectRadar({ onConnected }: { onConnected: () => void }) {
  const [email, setEmail] = useState("");
  const [account, setAccount] = useState<ConnectionCheck["account"] | null>(null);
  const [publication, setPublication] = useState<Publication>("private");
  const [connected, setConnected] = useState<string | null>(null);
  const radar = useRadarCheck(async (address) => {
    // An email input strips the spaces around what is typed.
    const checked = await api.adminCheckConnection(address, email);
    setAccount(checked.account);
    return checked;
  });
  const { probe, busy, refusal } = radar;

  async function connect() {
    const sent: { reply?: Awaited<ReturnType<typeof api.adminConnectPolledRadar>> } = {};
    await radar.confirm(async (address, fingerprint) => {
      sent.reply = await api.adminConnectPolledRadar({ address, fingerprint, publication, email });
    }, "Could not connect the radar. Try again.");
    // Unset when the connection was refused, with the refusal on the page.
    if (!sent.reply) return;
    const { node_id, owner } = sent.reply;
    setConnected(`Connected ${node_id} to ${owner.email}${owner.created ? ", on an account made for it" : ""}.`);
    radar.reset();
    setEmail("");
    setAccount(null);
    setPublication("private");
    onConnected();
  }

  if (!probe || !account) {
    return (
      <AddressCard
        title="Connect a radar"
        address={radar.address}
        busy={busy}
        refusal={refusal?.message ?? null}
        onChange={radar.setAddress}
        onCheck={radar.check}
        ready={LOOKS_LIKE_AN_ADDRESS.test(email)}
        lead={
          <>
            <label htmlFor="operator-email">Operator&rsquo;s email</label>
            <input
              id="operator-email"
              className="input"
              type="email"
              value={email}
              placeholder="operator@example.com"
              autoComplete="off"
              spellCheck={false}
              disabled={busy !== null}
              onChange={(e) => setEmail(e.target.value)}
            />
          </>
        }
      >
        {connected && (
          <p className="card-note" role="status">
            {connected}
          </p>
        )}
      </AddressCard>
    );
  }

  return (
    <>
      <DeclarationCard
        title="What the radar declares"
        probe={probe}
        busy={busy}
        refusal={refusal?.at === "check" ? refusal.message : null}
        onCheckAgain={radar.check}
        onChangeAddress={radar.changeAddress}
      />
      <div className="card">
        <div className="card-header">
          <h3>Connect it</h3>
        </div>
        <div className="card-body card-stack">
          {account.exists ? (
            <p>It goes to the account at {account.email}.</p>
          ) : (
            <div className="notice">
              <div className="notice-text">
                No account for {account.email}. One will be created for this address, with the radar on it from now.
                Check the spelling: whoever holds this mailbox gets the radar and its location.
              </div>
            </div>
          )}
          {!probe.protected && (
            <p className="card-note">
              The radar answers without a password, so anyone who finds its address can read it. It will be marked
              unprotected.
            </p>
          )}
          <PublicationOptions
            name="connect-publication"
            value={publication}
            disabled={busy !== null}
            onChange={setPublication}
          />
          <p className="card-note">
            It starts on probation, out of the solve, the archive and the public map until it is graduated. The
            operator sees it once they sign in with this address.
          </p>
          {radar.changed && (
            <p className="card-note">The radar&rsquo;s pins have changed: check them above before connecting it.</p>
          )}
          {refusal?.at === "confirm" && <Notice>{refusal.message}</Notice>}
          <div className="btn-row">
            <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={connect}>
              {busy === "confirming"
                ? "Connecting…"
                : account.exists
                  ? `Connect to ${account.email}`
                  : "Create account and connect"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
