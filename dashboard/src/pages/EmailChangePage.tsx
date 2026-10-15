import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { request, type CurrentUser } from "@retina/shared";

import { api } from "../api/client";
import LoginBack from "../components/LoginBack";
import { useAuth } from "../context/AuthContext";
import { describeFailure, linkUnspent, type RequestFailure } from "../utils/requestFailure";

/** What the page is showing. `asking` and `failed` are the states with buttons:
 *  every other one is terminal and says why. */
type Stage = "loading" | "asking" | "working" | "done" | "invalid" | "taken" | "failed";

/** A request that failed without the server saying anything about the link,
 *  and which one it was, so that Try again repeats it. */
interface Trouble {
  step: "preview" | "consume";
  failure: RequestFailure;
}

/** The link mailed to a new sign-in address, landed on.
 *
 *  Outside RequireAuth, deliberately: the link is the proof, and it may be
 *  opened in a browser that holds no session, or another account's. Confirming
 *  opens no session either way.
 *
 *  The new address is named before the button is pressed, and only the button
 *  spends the link: mail scanners fetch links, and one that spent it would
 *  leave the person a dead link on arrival.
 *
 *  Keyed by token, as the claim link's page is, so another link starts from
 *  nothing rather than inheriting the last one's address, or its answer.
 */
export default function EmailChangePage() {
  const { token = "" } = useParams();
  return <EmailChange key={token} token={token} />;
}

function EmailChange({ token }: { token: string }) {
  const { user, loading: identifying, updateUser } = useAuth();
  const [stage, setStage] = useState<Stage>("loading");
  const [address, setAddress] = useState<string | null>(null);
  const [trouble, setTrouble] = useState<Trouble | null>(null);
  // Counts the reads Try again has asked for, so each one runs the preview anew.
  const [reads, setReads] = useState(0);
  // False once the person has left the card, or another link has replaced it,
  // so an answer still in flight is dropped rather than drawn over what is there.
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let live = true;
    // The preview does not spend the link, so a page reloaded or opened twice
    // still works. Only the button below consumes anything.
    api.emailChangePreview(token).then(
      (body) => {
        if (!live) return;
        setAddress(body.email);
        setStage("asking");
      },
      (err) => {
        if (!live) return;
        const failure = describeFailure(err);
        // A 404 is the server's word that the link is dead. Anything else (a
        // 5xx, a 429, a dropped connection) says nothing about it.
        if (failure.status === 404) {
          setStage("invalid");
        } else {
          setTrouble({ step: "preview", failure });
          setStage("failed");
        }
      },
    );
    return () => {
      live = false;
    };
  }, [token, reads]);

  /** Takes the address the server holds for whoever is signed in, so the header
   *  and the account page stop showing the old one. Asked of the server rather
   *  than patched in: the link may belong to another account than the one here,
   *  and then what comes back is that account, unchanged. A failure leaves the
   *  old address on screen until the next load, and the change stands. */
  function refreshUser() {
    request<CurrentUser>("/api/auth/me").then(updateUser, () => {});
  }

  async function confirm() {
    setStage("working");
    try {
      const { email } = await api.consumeEmailChange(token);
      // Not gated on the card: the address has changed whether or not the
      // person is still looking at it.
      if (user) refreshUser();
      if (!mounted.current) return;
      setAddress(email);
      setStage("done");
    } catch (e) {
      if (!mounted.current) return;
      const failure = describeFailure(e);
      // 409 is the one refusal worth distinguishing: the link was real and the
      // address was taken while it sat in the inbox, which is not the same as a
      // link that never worked. A 400 is that link.
      if (failure.status === 409) {
        setStage("taken");
      } else if (failure.status === 400) {
        setStage("invalid");
      } else {
        setTrouble({ step: "consume", failure });
        setStage("failed");
      }
    }
  }

  function retry() {
    if (trouble?.step === "consume") confirm();
    else {
      setStage("loading");
      setReads((n) => n + 1);
    }
  }

  const away = user ? (
    <Link className="login-btn" to="/account">
      Go to your account
    </Link>
  ) : (
    <Link className="login-btn" to="/login">
      Sign in instead
    </Link>
  );

  return (
    <div className="login-page">
      <div className="login-card">
        {/* Mailed for the app host, as sign-in links are, so this card is never
            on the admin console. None while the confirm is in flight: the
            request goes on whether the person stays or not, and this card is
            where its answer is said. */}
        {stage !== "working" && <LoginBack isAdmin={false} />}
        <div className="logo">◉</div>
        <h1>Retina</h1>

        {/* Who is signed in decides where Continue goes and whether the header
            is refreshed, so the button waits until that is known. */}
        {(stage === "loading" || (stage === "asking" && identifying)) && (
          <p className="subtitle">Checking the link…</p>
        )}

        {stage === "asking" && !identifying && (
          <>
            <p className="subtitle">
              Use <strong>{address}</strong> to sign in to RETINA?
            </p>
            <p className="subtitle">If that was not you, close this page and nothing changes.</p>
            <button className="login-btn" onClick={confirm}>
              Yes, use this address
            </button>
          </>
        )}

        {stage === "working" && <p className="subtitle">One moment…</p>}

        {stage === "done" && (
          <>
            <p className="subtitle">
              Done. Your sign-in address is now <strong>{address}</strong>.
            </p>
            <Link className="login-btn" to={user ? "/account" : "/login"}>
              Continue
            </Link>
          </>
        )}

        {stage === "taken" && (
          <>
            <p className="login-error">That address now belongs to another account.</p>
            {away}
          </>
        )}

        {stage === "failed" && trouble && (
          <>
            <p className="login-error" role="alert">
              {trouble.failure.message}
            </p>
            {trouble.failure.retryable ? (
              <>
                {/* The confirm spends the link; the preview only reads it. */}
                <p className="login-note">
                  {linkUnspent(trouble.failure, { spends: trouble.step === "consume" })
                    ? "Your link has not been used."
                    : "Your link may still work."}
                </p>
                <button className="login-btn" onClick={retry}>
                  Try again
                </button>
              </>
            ) : (
              // The same request would meet the same answer.
              away
            )}
          </>
        )}

        {stage === "invalid" && (
          <>
            <p className="login-error">
              That link is no longer valid. Links last fifteen minutes and work once.
            </p>
            {away}
          </>
        )}
      </div>
    </div>
  );
}
