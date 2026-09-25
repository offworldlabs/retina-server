import { useState } from "react";
import { HttpError } from "@retina/shared";

import type { PolledRadarProbe } from "../types";

/** Where a refusal is shown: beside what it concerns, the address or the pins
 *  (`check`), or the button that sent the confirmation (`confirm`). */
type RadarRefusal = { at: "check" | "confirm"; message: string };

/**
 * Checking a stock blah2 radar at an address, then confirming what it
 * declares there: registering one and moving one to a new address are both
 * these two steps. `probeAt` is the check; the confirmation goes to confirm().
 */
export function useRadarCheck(probeAt: (address: string) => Promise<PolledRadarProbe>) {
  const [address, setAddress] = useState("");
  const [probe, setProbe] = useState<PolledRadarProbe | null>(null);
  const [busy, setBusy] = useState<"checking" | "confirming" | null>(null);
  const [refusal, setRefusal] = useState<RadarRefusal | null>(null);
  // The radar's declaration changed under the owner and came back to be checked.
  const [changed, setChanged] = useState(false);

  async function check() {
    setRefusal(null);
    setChanged(false);
    setBusy("checking");
    try {
      setProbe(await probeAt(address.trim()));
    } catch (err) {
      setRefusal({ at: "check", message: (err as Error).message || "Could not check the radar. Try again." });
    } finally {
      setBusy(null);
    }
  }

  /** Send the fingerprint of what the owner was shown. True once it has
   *  landed, with the hook still busy until the page moves on or calls
   *  reset(); false with the refusal on the page. */
  async function confirm(
    send: (address: string, fingerprint: string) => Promise<unknown>,
    fallback: string,
  ): Promise<boolean> {
    if (!probe) return false;
    setRefusal(null);
    setChanged(false);
    setBusy("confirming");
    try {
      await send(address.trim(), probe.fingerprint);
      return true;
    } catch (err) {
      const body = err instanceof HttpError ? (err.body as { code?: string; probe?: PolledRadarProbe } | null) : null;
      const message = (err as Error).message || fallback;
      if (body?.code === "config_changed" && body.probe) {
        // What the radar declares now, for the owner to confirm in its place.
        setProbe(body.probe);
        setChanged(true);
        setRefusal({ at: "check", message });
      } else {
        setRefusal({ at: "confirm", message });
      }
      setBusy(null);
      return false;
    }
  }

  /** Back to the address, which stays as typed. */
  function changeAddress() {
    setProbe(null);
    setRefusal(null);
    setChanged(false);
  }

  function reset() {
    changeAddress();
    setAddress("");
    setBusy(null);
  }

  return { address, setAddress, probe, busy, refusal, changed, check, confirm, changeAddress, reset };
}
