import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { api } from "../api/client";
import LoginPage from "./LoginPage";

/** The server's single answer for unknown, expired and already-redeemed. */
const DEAD_LINK = "That sign-in link is no longer valid";

/** The page the mailed link opens. The GET that got here consumed nothing —
 *  mail providers prefetch links, and a token spent by a scanner is a link
 *  that is already dead when its recipient clicks it — so the redemption is
 *  this POST, made once the page is in front of a person. */
export default function AuthLinkPage() {
  const { token } = useParams();
  const navigate = useNavigate();
  const { signIn } = useAuth();
  const [failure, setFailure] = useState(null);
  const [attempt, setAttempt] = useState(0);
  // The token dies on the first POST, so a second would report a dead link for
  // a sign-in that had just succeeded. Keyed by token rather than a bare flag:
  // this must survive a re-run of the effect for the same link and still
  // redeem a different one.
  const redeemed = useRef(null);

  useEffect(() => {
    if (redeemed.current === token) return;
    redeemed.current = token;
    (async () => {
      try {
        const { user } = await api.consumeMagicLink(token);
        // The answer carries the identity the session now has, so the app is
        // told directly; left to discover it, the guard would bounce a valid
        // session back to the login card.
        signIn(user);
        navigate("/", { replace: true });
      } catch (err) {
        // 400 is the server's one answer for unknown, expired and
        // already-redeemed, and it is final. Anything else — a 5xx, a dropped
        // connection — says nothing about the token, and sending the person off
        // to request another throws away a link that still works. Unlike the
        // request form, there is no reason to blur the two: whoever is here
        // already holds the token.
        if (err?.status === 400) {
          setFailure("dead");
          return;
        }
        // Nothing was consumed, so the same link may be offered again.
        redeemed.current = null;
        setFailure("transient");
      }
    })();
  }, [token, signIn, navigate, attempt]);

  if (failure === "dead") return <LoginPage message={DEAD_LINK} />;

  if (failure === "transient") {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="logo">◉</div>
          <h1>Retina</h1>
          <p className="login-error">We could not reach the server to sign you in.</p>
          <p className="login-note">Your link has not been used. Try again.</p>
          <button
            className="login-btn"
            onClick={() => {
              setFailure(null);
              setAttempt((n) => n + 1);
            }}
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  return <div className="loading-screen">Signing you in…</div>;
}
