import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { api } from "../api/client";
import { useFetch } from "../hooks/usePolling";
import { signInNext } from "../utils/signInNext";
import LoginPage from "./LoginPage";
import LoginBack from "../components/LoginBack";

/** The server's single answer for unknown, expired and already-redeemed. */
const DEAD_LINK = "That sign-in link is no longer valid";

type Stage = "checking" | "asking" | "signing-in" | "dead" | "unreachable" | "throttled";

/** What went wrong and what to do, for each failure that leaves the link alive. */
const TROUBLE = {
  unreachable: ["We could not reach the server to sign you in.", "Your link has not been used. Try again."],
  throttled: ["Too many sign-in attempts from this network.", "Your link has not been used. Wait a minute, then try again."],
} as const;

/** The page the mailed link opens. The token is spent by a POST made only when
 *  the person presses the button: mail scanners fetch links and some render
 *  the page and run its script, and a token spent by one is a link that is
 *  already dead when its recipient clicks it. Until then the page reads the
 *  link without spending it, to name the address it signs in as.
 *
 *  Keyed by token, so another link starts from nothing rather than inheriting
 *  the last one's address. */
export default function AuthLinkPage({ isAdmin = false }) {
  const { token = "" } = useParams();
  return <SignInLink key={token} token={token} isAdmin={isAdmin} />;
}

function SignInLink({ token, isAdmin }: { token: string; isAdmin: boolean }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // The page asked for before signing in, which the mailed link carries. None
  // on the admin console, which the mailed link never opens on.
  const next = isAdmin ? null : signInNext(searchParams.get("next"));
  const { signIn } = useAuth();
  // A dead link is the preview's commonest answer, so it resolves as no address
  // rather than failing: useFetch logs failures, and only the unexpected ones
  // (a 5xx, a 429, a dropped connection) are worth that.
  const preview = useFetch<{ email: string | null }>(
    () =>
      api.previewMagicLink(token).catch((err) => {
        if (err?.status === 404) return { email: null };
        throw err;
      }),
    token,
  );
  // What pressing Sign in has come to; null until it is pressed.
  const [redemption, setRedemption] = useState<Stage | null>(null);
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

  const email = preview.data?.email ?? null;
  const stage: Stage =
    redemption ??
    (preview.pending ? "checking" : preview.error ? failure(preview.error, 404) : email === null ? "dead" : "asking");

  async function confirm() {
    if (redeeming.current) return;
    redeeming.current = true;
    setRedemption("signing-in");
    try {
      const { user } = await api.consumeMagicLink(token);
      if (!mounted.current) return;
      // The answer carries the identity the session now has, so the app is
      // told directly; left to discover it, the guard would bounce a valid
      // session back to the login card.
      signIn(user);
      navigate(next ?? "/", { replace: true });
    } catch (err) {
      if (mounted.current) setRedemption(failure(err, 400));
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

  if (stage === "dead") return <LoginPage message={DEAD_LINK} isAdmin={isAdmin} />;

  if (stage === "unreachable" || stage === "throttled") {
    const [problem, advice] = TROUBLE[stage];
    return (
      <Card isAdmin={isAdmin}>
        <p className="login-error">{problem}</p>
        <p className="login-note">{advice}</p>
        <button className="login-btn" onClick={retry}>
          Try again
        </button>
      </Card>
    );
  }

  if (stage === "asking") {
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

  return <div className="loading-screen">{stage === "checking" ? "Checking your link…" : "Signing you in…"}</div>;
}

/** Why a request about the link failed. `final` is the status the server gives
 *  for unknown, expired and already-redeemed: the preview's 404, the
 *  redemption's 400. Anything else (a 5xx, a dropped connection) says nothing
 *  about the token, and sending the person off to request another throws away a
 *  link that still works. Unlike the request form, there is no reason to blur
 *  the two: whoever is here already holds the token. */
function failure(err: unknown, final: number): Stage {
  const status = (err as { status?: number } | null)?.status;
  if (status === final) return "dead";
  // nginx's per-address limit on credential endpoints. Retrying at once only
  // meets it again, so it is not reported as the server being down.
  if (status === 429) return "throttled";
  return "unreachable";
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
