import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { api } from "../api/client";
import LoginBack from "../components/LoginBack";

/** The only two failures this page repeats back. Both describe the request or
 *  the deployment; neither says anything about the address. */
const INVALID_EMAIL = "Enter a valid email address";
const MAIL_UNAVAILABLE = "Sign-in by email is unavailable";

export default function LoginPage({ message = null, isAdmin = false }) {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState(null);

  if (user) {
    navigate("/", { replace: true });
    return null;
  }

  async function submit(e) {
    e.preventDefault();
    setError(null);
    setSending(true);
    try {
      await api.requestMagicLink(email);
    } catch (err) {
      const status = err?.status;
      if (status === 422 || status === 503) {
        setError(status === 422 ? INVALID_EMAIL : MAIL_UNAVAILABLE);
        setSending(false);
        return;
      }
      // Every other failure is answered with the confirmation below, the same
      // as a send that worked. The server already refuses to say whether an
      // address has an account; a page that showed its own errors here would
      // give that back by the difference between the two screens.
    }
    setSending(false);
    setSent(true);
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <LoginBack isAdmin={isAdmin} />
        <div className="logo">◉</div>
        <h1>Retina</h1>
        <p className="subtitle">Passive Radar Network Dashboard</p>
        {message && <p className="login-error">{message}</p>}
        {sent ? (
          <>
            <p className="login-note">
              If that address has an account, a sign-in link is on its way.
            </p>
            <p className="login-note">The link works once and expires in 15 minutes.</p>
            <button
              type="button"
              className="login-btn"
              onClick={() => {
                setSent(false);
                setEmail("");
              }}
            >
              Use a different address
            </button>
          </>
        ) : (
          // noValidate: the server decides what an address is, and its 422 is
          // shown inline. Letting the browser refuse a submit as well would
          // mean two judgements of validity that can disagree.
          <form className="login-form" onSubmit={submit} noValidate>
            <label htmlFor="login-email">Email address</label>
            <input
              id="login-email"
              className="login-input"
              type="email"
              name="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            {error && <p className="login-error">{error}</p>}
            <button type="submit" className="login-btn" disabled={sending}>
              {sending ? "Sending…" : "Email me a sign-in link"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
