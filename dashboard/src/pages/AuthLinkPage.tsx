import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { api } from "../api/client";
import { useFetch } from "../hooks/usePolling";
import { signInNext } from "../utils/signInNext";
import { describeFailure, type RequestFailure } from "../utils/requestFailure";
import LoginPage from "./LoginPage";
import LoginBack from "../components/LoginBack";

/** The server's single answer for unknown, expired and already-redeemed. */
const DEAD_LINK = "That sign-in link is no longer valid";

/** What reading the link came to. A dead link and a failed read both resolve
 *  rather than reject. A dead link is the preview's commonest answer, not a
 *  failure worth useFetch's log. A failure is described here, from the
 *  rejection the request made: useFetch hands back an Error, and re-wraps
 *  anything that is not one of this realm's, which loses the name the helper
 *  tells a timeout by. */
interface Preview {
  email: string | null;
  failure: RequestFailure | null;
}

/** What pressing a sign-in button has come to. */
type Redemption = { stage: "signing-in" } | { stage: "dead" } | { stage: "failed"; failure: RequestFailure };

interface AuthLinkPageProps {
  isAdmin?: boolean;
}

/** The page the mailed link opens. The token is spent by a POST made only when
 *  the person presses the button: mail scanners fetch links and some render
 *  the page and run its script, and a token spent by one is a link that is
 *  already dead when its recipient clicks it. Until then the page reads the
 *  link without spending it, to name the address it signs in as.
 *
 *  Redeeming replaces whatever session the browser holds, so the page also
 *  asks who is here before it offers anything: a person already signed in as
 *  another address is asked which account they want, rather than finding
 *  theirs swapped for the link's.
 *
 *  Keyed by token, so another link starts from nothing rather than inheriting
 *  the last one's address. */
export default function AuthLinkPage({ isAdmin = false }: AuthLinkPageProps) {
  const { token = "" } = useParams();
  return <SignInLink key={token} token={token} isAdmin={isAdmin} />;
}

function SignInLink({ token, isAdmin }: { token: string; isAdmin: boolean }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // The page asked for before signing in, which the mailed link carries. None
  // on the admin console, which the mailed link never opens on.
  const next = isAdmin ? null : signInNext(searchParams.get("next"));
  const destination = next ?? "/";
  const { user, loading: identifying, signIn } = useAuth();
  const preview = useFetch<Preview>(
    () =>
      api.previewMagicLink(token).then(
        (body) => ({ email: body?.email ?? null, failure: null }),
        (err) => {
          const failure = describeFailure(err);
          if (failure.status === 404) return { email: null, failure: null };
          console.error(err);
          return { email: null, failure };
        },
      ),
    token,
  );
  // What pressing a sign-in button has come to; null until one is pressed.
  const [redemption, setRedemption] = useState<Redemption | null>(null);
  // False once another link has replaced this one, so a redemption still in
  // flight does not sign in or navigate from under that link's question.
  const mounted = useRef(false);
  // One redemption at a time: the token is single-use, so a second request
  // would lose with a 400 and could report a dead link over a sign-in the
  // first had completed.
  const redeeming = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function confirm() {
    if (redeeming.current) return;
    redeeming.current = true;
    setRedemption({ stage: "signing-in" });
    try {
      // The server replaces any session the browser already holds.
      const { user: signedIn } = await api.consumeMagicLink(token);
      if (!mounted.current) return;
      // The answer carries the identity the session now has, so the app is
      // told directly; left to discover it, the guard would bounce a valid
      // session back to the login card.
      signIn(signedIn);
      navigate(destination, { replace: true });
    } catch (err) {
      if (!mounted.current) return;
      // A 400 is the server's final word on the token. Anything else (a 5xx,
      // a 429, a dropped connection) says nothing about it, and sending the
      // person off to request another would throw away a link that works.
      const failure = describeFailure(err);
      setRedemption(failure.status === 400 ? { stage: "dead" } : { stage: "failed", failure });
    } finally {
      redeeming.current = false;
    }
  }

  function retry() {
    // A failed press is retried as the press: the person has already said yes.
    if (redemption !== null) {
      confirm();
      return;
    }
    preview.refresh();
  }

  // Nothing is decided until both the link and the session have answered:
  // which question to ask depends on who is here as much as on the link.
  const read = preview.pending || identifying ? null : preview.data;
  const stage =
    redemption?.stage ?? (read === null ? "checking" : read.failure ? "failed" : read.email === null ? "dead" : "asking");
  const failure = redemption?.stage === "failed" ? redemption.failure : (read?.failure ?? null);
  const email = read?.email ?? null;

  if (stage === "dead") {
    // The login card would only send someone with a session on without a word,
    // leaving them to think the link had worked.
    if (!user) return <LoginPage message={DEAD_LINK} isAdmin={isAdmin} />;
    return (
      <Card isAdmin={isAdmin}>
        <p className="login-error" role="alert">
          {DEAD_LINK}
        </p>
        <p className="login-note">
          You are still signed in as <strong>{user.email}</strong>.
        </p>
        <Link className="login-btn" to={destination} replace>
          Continue
        </Link>
      </Card>
    );
  }

  if (stage === "failed" && failure) {
    // Unspent for certain only when nothing that spends it can have run: the
    // preview reads the link, and nginx's 429 refuses a redemption before the
    // app sees it. Any other failed redemption may have reached the app, which
    // spends the token before it opens the session; if it did, the retry meets
    // the dead-link answer and says so.
    const unspent = redemption === null || failure.kind === "throttled";
    return (
      <Card isAdmin={isAdmin}>
        <p className="login-error" role="alert">
          {failure.message}
        </p>
        {failure.retryable ? (
          <>
            <p className="login-note">{unspent ? "Your link has not been used." : "Your link may still work."}</p>
            <button className="login-btn" onClick={retry}>
              Try again
            </button>
          </>
        ) : (
          // The same request would meet the same answer, so the way on is a
          // new link, asked for the page this one was going to open.
          <Link className="login-btn" to="/login" state={next ? { next } : undefined}>
            Go to sign in
          </Link>
        )}
      </Card>
    );
  }

  if (stage === "asking" && !user) {
    return (
      <Card isAdmin={isAdmin}>
        <p className="subtitle">
          Sign in as <strong>{email}</strong>?
        </p>
        <button className="login-btn" onClick={confirm}>
          Sign in
        </button>
      </Card>
    );
  }

  if (stage === "asking" && sameAddress(user.email, email)) {
    // Redeeming would only spend a link that opens the session already here.
    return (
      <Card isAdmin={isAdmin}>
        <p className="subtitle">
          You are already signed in as <strong>{user.email}</strong>.
        </p>
        <Link className="login-btn" to={destination} replace>
          Continue
        </Link>
      </Card>
    );
  }

  if (stage === "asking") {
    return (
      <Card isAdmin={isAdmin}>
        <p className="subtitle">
          You are signed in as <strong>{user.email}</strong>. This link signs in as <strong>{email}</strong>.
        </p>
        <button className="login-btn" onClick={confirm}>
          Sign in as {email}
        </button>
        <Link className="login-btn secondary" to={destination} replace>
          Stay signed in as {user.email}
        </Link>
      </Card>
    );
  }

  return <div className="loading-screen">{stage === "checking" ? "Checking your link…" : "Signing you in…"}</div>;
}

/** Whether two addresses name one account, by the rule the server keys
 *  accounts on (normalise_email in core/users.py): case and surrounding spaces
 *  aside. */
function sameAddress(a: string | null | undefined, b: string | null | undefined) {
  return (a ?? "").trim().toLowerCase() === (b ?? "").trim().toLowerCase();
}

function Card({ isAdmin, children }: { isAdmin: boolean; children: ReactNode }) {
  return (
    <div className="login-page">
      <div className="login-card">
        <LoginBack isAdmin={isAdmin} />
        <div className="logo">◉</div>
        <h1>Retina</h1>
        {children}
      </div>
    </div>
  );
}
