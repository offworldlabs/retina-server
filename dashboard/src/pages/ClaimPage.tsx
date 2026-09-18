import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { api } from "../api/client";
import { useAuth } from "../context/AuthContext";

/** What the page is showing. `asking` is the only state with buttons: every
 *  other one is terminal and says why. */
type Stage = "loading" | "asking" | "working" | "bound" | "declined" | "invalid" | "taken";

/** The link a node's owner was mailed, landed on.
 *
 *  Outside RequireAuth, deliberately: whoever clicked this may have no account
 *  yet, and one click is meant to create it, bind the node and sign them in.
 *
 *  The node is named before either button is pressed. Somebody who was mailed
 *  this because a stranger mistyped an address needs to be declining a thing
 *  they can see, not a token.
 */
export default function ClaimPage() {
  const { token = "" } = useParams();
  const navigate = useNavigate();
  const { signIn } = useAuth();
  const [stage, setStage] = useState<Stage>("loading");
  const [nodeRef, setNodeRef] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    // The preview does not spend the link, so a page reloaded or opened twice
    // still works. Only the buttons below consume anything.
    api
      .claimPreview(token)
      .then((body) => {
        if (!live) return;
        setNodeRef(body.node_ref);
        setStage("asking");
      })
      .catch(() => {
        if (live) setStage("invalid");
      });
    return () => {
      live = false;
    };
  }, [token]);

  async function confirm() {
    setStage("working");
    try {
      const { user } = await api.consumeClaim(token);
      // The answer carries the identity the session now has, so the app is
      // told directly; left to discover it, the guard would go on treating a
      // fresh session as signed out until a reload.
      signIn(user);
      setStage("bound");
      // To the new owner's own nodes: the index opens on the map instead.
      window.setTimeout(() => navigate("/overview", { replace: true }), 1200);
    } catch (e) {
      // 409 is the one refusal worth distinguishing: the link was real and the
      // node was claimed by somebody else while it sat in the inbox, which is
      // not the same as a link that never worked.
      setStage((e as { status?: number })?.status === 409 ? "taken" : "invalid");
    }
  }

  async function decline() {
    setStage("working");
    try {
      await api.declineClaim(token);
      setStage("declined");
    } catch {
      setStage("invalid");
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="logo">◉</div>
        <h1>Retina</h1>

        {stage === "loading" && <p className="subtitle">Checking the link…</p>}

        {stage === "asking" && (
          <>
            <p className="subtitle">
              Someone asked to connect node <strong>{nodeRef}</strong> to this email address.
            </p>
            <p className="subtitle">
              If that was you, confirm below. If you do not recognise it, decline and nothing happens.
            </p>
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

        {stage === "invalid" && (
          <>
            <p className="login-error">
              That link is no longer valid. Links last fifteen minutes and work once.
            </p>
            <p className="subtitle">Ask the node to send another, then use the newest email.</p>
            <a className="login-btn" href="/login">
              Sign in instead
            </a>
          </>
        )}
      </div>
    </div>
  );
}
