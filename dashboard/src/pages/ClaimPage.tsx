import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api/client";
import LoginBack from "../components/LoginBack";
import { useAuth } from "../context/AuthContext";
import { describeFailure, linkUnspent, type RequestFailure } from "../utils/requestFailure";

/** What the page is showing. `asking` and `failed` are the states with buttons:
 *  every other one is terminal and says why. */
type Stage = "loading" | "asking" | "working" | "bound" | "declined" | "invalid" | "taken" | "failed";

/** A request that failed without the server saying anything about the link,
 *  and which one it was, so that Try again repeats it. */
interface Trouble {
  step: "preview" | "consume" | "decline";
  failure: RequestFailure;
}

/** How long the new owner reads that the node is theirs before it opens. */
const BOUND_REDIRECT_MS = 1200;

/** The link a node's owner was mailed, landed on.
 *
 *  Outside RequireAuth, deliberately: whoever clicked this may have no account
 *  yet, and one click is meant to create it, bind the node and sign them in.
 *
 *  The node is named before either button is pressed. Somebody who was mailed
 *  this because a stranger mistyped an address needs to be declining a thing
 *  they can see, not a token.
 *
 *  Keyed by token, as the sign-in link's page is, so another link starts from
 *  nothing rather than inheriting the last one's node, or its answer.
 */
export default function ClaimPage() {
  const { token = "" } = useParams();
  return <Claim key={token} token={token} />;
}

function Claim({ token }: { token: string }) {
  const navigate = useNavigate();
  const { user, loading: identifying, signIn, whenSignedOut } = useAuth();
  const [stage, setStage] = useState<Stage>("loading");
  const [nodeRef, setNodeRef] = useState<string | null>(null);
  const [trouble, setTrouble] = useState<Trouble | null>(null);
  // Counts the reads Try again has asked for, so each one runs the preview anew.
  const [reads, setReads] = useState(0);
  // False once the person has left the card, or another link has replaced it,
  // so an answer still in flight is dropped: a confirm's would sign in from
  // under the page or the question that is there instead.
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
    // still works. Only the buttons below consume anything.
    api.claimPreview(token).then(
      (body) => {
        if (!live) return;
        setNodeRef(body.node_ref);
        setStage("asking");
      },
      (err) => {
        if (!live) return;
        const failure = describeFailure(err);
        // A 404 is the server's word that the link is dead. Anything else (a
        // 5xx, a 429, a dropped connection) says nothing about it, and calling
        // it dead would send the owner off to have the node mail them again.
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

  // Owned by the bound card, so leaving it first (Back, or any other route)
  // clears the redirect, rather than letting it pull the person off whatever
  // page they went to instead.
  useEffect(() => {
    if (stage !== "bound") return;
    // To the new owner's own nodes: the index opens on the map instead.
    const timer = window.setTimeout(() => navigate("/overview", { replace: true }), BOUND_REDIRECT_MS);
    return () => window.clearTimeout(timer);
  }, [stage, navigate]);

  function fail(step: Trouble["step"], failure: RequestFailure) {
    setTrouble({ step, failure });
    setStage("failed");
  }

  async function confirm() {
    setStage("working");
    try {
      // After any sign-out in flight: its answer deletes the session cookie,
      // and landing after this one's would end the session this opens.
      await whenSignedOut();
      if (!mounted.current) return;
      const { user } = await api.consumeClaim(token);
      if (!mounted.current) return;
      // The answer carries the identity the session now has, so the app is
      // told directly; left to discover it, the guard would go on treating a
      // fresh session as signed out until a reload.
      signIn(user);
      setStage("bound");
    } catch (e) {
      if (!mounted.current) return;
      const failure = describeFailure(e);
      // 409 is the one refusal worth distinguishing: the link was real and the
      // node was claimed by somebody else while it sat in the inbox, which is
      // not the same as a link that never worked. A 400 is that link.
      if (failure.status === 409) setStage("taken");
      else if (failure.status === 400) setStage("invalid");
      else fail("consume", failure);
    }
  }

  async function decline() {
    setStage("working");
    try {
      await api.declineClaim(token);
      if (!mounted.current) return;
      setStage("declined");
    } catch (e) {
      if (!mounted.current) return;
      const failure = describeFailure(e);
      if (failure.status === 400) setStage("invalid");
      else fail("decline", failure);
    }
  }

  function retry() {
    if (trouble?.step === "consume") confirm();
    else if (trouble?.step === "decline") decline();
    else {
      setStage("loading");
      setReads((n) => n + 1);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        {/* Claim links are mailed for the app host, as sign-in links are, so
            this card is never on the admin console. None while a confirm or
            a decline is in flight, as the sign-in link's card offers none
            while it signs in: the request goes on whether the person stays
            or not, and this card is where its answer is said. */}
        {stage !== "working" && <LoginBack isAdmin={false} />}
        <div className="logo">◉</div>
        <h1>Retina</h1>

        {/* Confirming opens a session for the account the link was mailed to,
            in place of any this browser holds, and the preview does not name
            that account, so the card cannot tell a switch from a stay. What
            it can say is who is signed in now, so the buttons wait until
            that is known. */}
        {(stage === "loading" || (stage === "asking" && identifying)) && (
          <p className="subtitle">Checking the link…</p>
        )}

        {stage === "asking" && !identifying && (
          <>
            <p className="subtitle">
              Someone asked to connect node <strong>{nodeRef}</strong> to this email address.
            </p>
            <p className="subtitle">
              If that was you, confirm below. If you do not recognise it, decline and nothing happens.
            </p>
            {user && (
              <p className="login-note">
                You are signed in as <strong>{user.email}</strong>. Confirming signs this browser in to the
                account this link was sent to.
              </p>
            )}
            <button className="login-btn" onClick={confirm}>
              Yes, this is my node
            </button>
            <button className="login-btn secondary" onClick={decline}>
              I did not ask for this
            </button>
          </>
        )}

        {stage === "working" && <p className="subtitle">One moment…</p>}

        {stage === "bound" && (
          <p className="subtitle">
            Node <strong>{nodeRef}</strong> is yours. Taking you to your dashboard…
          </p>
        )}

        {stage === "declined" && (
          <p className="subtitle">
            Declined. Node <strong>{nodeRef}</strong> was not connected to this address, and nobody has
            been told who you are.
          </p>
        )}

        {stage === "taken" && (
          <p className="login-error">
            That node already belongs to someone else. If you bought it second hand, the previous owner
            needs to release it first.
          </p>
        )}

        {stage === "failed" && trouble && (
          <>
            <p className="login-error" role="alert">
              {trouble.failure.message}
            </p>
            {trouble.failure.retryable ? (
              <>
                {/* A confirm and a decline both spend the link; the preview
                    only reads it. */}
                <p className="login-note">
                  {linkUnspent(trouble.failure, { spends: trouble.step !== "preview" })
                    ? "Your link has not been used."
                    : "Your link may still work."}
                </p>
                <button className="login-btn" onClick={retry}>
                  Try again
                </button>
              </>
            ) : (
              // The same request would meet the same answer.
              <Link className="login-btn" to="/login">
                Sign in instead
              </Link>
            )}
          </>
        )}

        {stage === "invalid" && (
          <>
            <p className="login-error">
              That link is no longer valid. Links last fifteen minutes and work once.
            </p>
            <p className="subtitle">Ask the node to send another, then use the newest email.</p>
            <Link className="login-btn" to="/login">
              Sign in instead
            </Link>
          </>
        )}
      </div>
    </div>
  );
}
