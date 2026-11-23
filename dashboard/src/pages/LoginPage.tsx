import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useSearchParams } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { api } from "../api/client";
import LoginBack from "../components/LoginBack";
import { signInNext } from "../utils/signInNext";
import { describeFailure, type RequestFailure } from "../utils/requestFailure";

/** What this card says in place of the helper's own words, for the two
 *  failures it knows more about than a status does: a form with one field, and
 *  the one dependency a sign-in link has. */
const INVALID_EMAIL = "Enter a valid email address";
const MAIL_UNAVAILABLE = "Sign-in by email is unavailable";

const ERROR_ID = "login-email-error";

interface LoginPageProps {
  /** Why the card is showing, such as the reason a mailed link did not work. */
  message?: string | null;
  isAdmin?: boolean;
}

export default function LoginPage({ message = null, isAdmin = false }: LoginPageProps) {
  const { user } = useAuth();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  // The page the visitor was on their way to: handed over by whatever sent them
  // here, or, when a dead link renders this card, carried in that link's URL.
  // None on the admin console, whose routes the mailed link cannot open.
  const next = isAdmin ? null : signInNext(location.state?.next ?? searchParams.get("next"));
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<RequestFailure | null>(null);

  // Rendered rather than called: a navigation made while rendering updates the
  // router in the middle of this component's render, and the router drops one
  // made on the first render altogether, which left a blank card.
  if (user) return <Navigate to={next ?? "/"} replace />;

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setFailure(null);
    setSending(true);
    try {
      await api.requestMagicLink(email, next);
      setSent(true);
    } catch (err) {
      // The server answers every address alike, with a 202 that covers its own
      // quota as well, so a failure here describes the request or the
      // deployment, never the address. Saying what it was tells nobody which
      // addresses have accounts, and a confirmation in its place would have the
      // person wait for mail that is not coming.
      setFailure(describeFailure(err, { invalid: INVALID_EMAIL, unavailable: MAIL_UNAVAILABLE }));
    } finally {
      setSending(false);
    }
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
            <p className="login-note">A sign-in link is on its way to {email}.</p>
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
              inputMode="email"
              enterKeyHint="send"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              // Invalid only when the server said the address was; a rate limit
              // or a dropped connection is no fault of what was typed.
              aria-invalid={failure?.kind === "invalid" || undefined}
              aria-describedby={failure ? ERROR_ID : undefined}
            />
            {failure && (
              <p id={ERROR_ID} className="login-error" role="alert">
                {failure.message}
              </p>
            )}
            <button type="submit" className="login-btn" disabled={sending}>
              {sending ? "Sending…" : "Email me a sign-in link"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
