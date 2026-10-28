import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import DashboardLayout from "../components/DashboardLayout";
import RequireAuth from "../components/RequireAuth";
import { AuthProvider, useAuth } from "../context/AuthContext";
import { ThemeProvider } from "../context/ThemeContext";
import { useOwnedNodes } from "../hooks/useOwnedNodes";
import AuthLinkPage from "../pages/AuthLinkPage";
import ClaimPage from "../pages/ClaimPage";
import type { OwnedNode } from "../types";
import { ownedNode } from "./fixtures/ownedNode";
import { stubMatchMedia } from "./matchMedia";

/**
 * An owner route's 401 that answers after this document has moved on to
 * another session.
 *
 * A request carries the session cookie it left with. Signing out and in again
 * through a mailed link happens in the same document, so a request sent under
 * the session before can still be out when the next is adopted, and answer 401
 * because the old token has run out. That answer is about the session it was
 * sent under: it must neither end the new one here nor mark the new owner's
 * nodes signed out. A 401 to a request sent under the session held now still
 * ends it.
 *
 * And a 401 that lands while this session's own sign-out is out: the sign-out
 * is ending it already, so it ends once, says nothing more, and whatever waits
 * for it is let go when it answers.
 *
 * The real provider, client, guard, layout, mailed-link pages and owned-nodes
 * hook are used, with fetch stubbed as the server and the browser's cookie jar.
 */

const DAY = "2026-09-17";
const ADA = { id: "u-ada", name: "Ada", email: "ada@example.com", role: "user" };
const PILOT = { id: "u-pilot", name: "Pilot", email: "pilot@example.com", role: "user" };
type Account = typeof ADA;

const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
// The node Pilot's claim link binds.
const KITE = ownedNode({ node_id: "retkite0001", node_ref: "ndekite00000001", name: "Kite" });
const NODES: Record<string, OwnedNode[]> = { [ADA.id]: [ROOF, MAST], [PILOT.id]: [KITE] };

const NODES_PATH = "/api/auth/me/nodes";

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const unauthenticated = () => json(401, { detail: "Not authenticated" });

type Reply = () => Response | Promise<Response>;

const server = {
  /** The tokens the server accepts, and whose they are. It keeps no sessions,
   *  as the real one keeps none: a token is good until it runs out, and signing
   *  out only has the browser drop it. */
  tokens: new Map<string, Account>(),
  /** The browser's session cookie: the token a request carries as it leaves. */
  cookie: null as string | null,
  issued: 0,
  /** When requests left and answers landed, in order. */
  log: [] as string[],
  /** Requests held by path, each answered once the test lets it go and worked
   *  out then, as a request the server reaches late would be. */
  holds: new Map<string, Promise<void>>(),
  /** Set to hold the sign-out's answer until the test gives it one. */
  signOut: null as Promise<Reply> | null,
};

/** A new session for `account`, in place of any the browser held. */
function open(account: Account) {
  const token = `${account.id}#${++server.issued}`;
  server.tokens.set(token, account);
  server.cookie = token;
  return token;
}

/** The server stops taking `token`, as it does one past its lifetime. */
function runOut(token: string | null) {
  if (token) server.tokens.delete(token);
}

async function serve(url: string, init: RequestInit = {}): Promise<Response> {
  const path = String(url);
  const post = init.method === "POST";
  const token = server.cookie;
  const hold = server.holds.get(path);
  if (hold) {
    server.holds.delete(path);
    await hold;
  }
  const account = token ? server.tokens.get(token) : undefined;
  if (path === "/api/auth/me") return account ? json(200, account) : unauthenticated();
  if (path === NODES_PATH) {
    if (hold) server.log.push(`late ${account ? 200 : 401} ${path}`);
    return account ? json(200, NODES[account.id]) : unauthenticated();
  }
  if (post && path === "/api/auth/logout") {
    // Its Set-Cookie drops the token when a confirmed answer lands, and only
    // then.
    server.log.push("sign-out sent");
    const reply = server.signOut ? await server.signOut : () => json(200, { ok: true });
    server.log.push("sign-out answered");
    const response = await reply();
    if (response.ok) server.cookie = null;
    return response;
  }
  if (post && (path === "/api/auth/magic-link/consume" || path === "/api/auth/claim/consume")) {
    server.log.push("redemption sent");
    open(PILOT);
    return json(200, { user: PILOT, node_ref: KITE.node_ref });
  }
  if (path === "/api/auth/magic-link/tok") return json(200, { email: PILOT.email });
  if (path === "/api/auth/claim/tok") return json(200, { node_ref: KITE.node_ref });
  return json(404, { detail: `unexpected request: ${path}` });
}

/** Holds the next request to the owner's list. The returned function lets it
 *  answer and waits until the client has had the answer. */
function holdNextList() {
  let release = () => {};
  server.holds.set(NODES_PATH, new Promise<void>((resolve) => (release = resolve)));
  return async (expected: number) => {
    release();
    await waitFor(() => expect(server.log).toContain(`late ${expected} ${NODES_PATH}`));
    await elapse(1000);
  };
}

/** Holds the sign-out's answer. The returned function gives it one. */
function holdSignOut() {
  let answer: (reply: Reply) => void = () => {};
  server.signOut = new Promise<Reply>((resolve) => (answer = resolve));
  return (reply: Reply) =>
    act(async () => {
      answer(reply);
    });
}

const paths = () => (fetch as Mock).mock.calls.map(([url]) => String(url));
const asked = (path: string) => paths().filter((p) => p === path).length;

const realLocation = window.location;

/** The page the client reads to decide whether to send the caller away. */
function stubLocation(pathname: string, hostname = "app.retina.fm") {
  const loc = {
    pathname,
    hostname,
    host: hostname,
    protocol: "https:",
    origin: `https://${hostname}`,
    search: "",
    hash: "",
    href: `https://${hostname}${pathname}`,
    assign: vi.fn(),
    replace: vi.fn(),
    reload: vi.fn(),
  };
  Object.defineProperty(window, "location", { value: loc, writable: true, configurable: true });
  return loc;
}

function Identity() {
  const { user, signingOut } = useAuth();
  return (
    <>
      <output aria-label="identity">{user ? user.email : "nobody"}</output>
      <output aria-label="signing out">{String(signingOut)}</output>
    </>
  );
}

/** The owner's nodes as a page lists them, and the two mailed links. */
function OwnedPage() {
  const owned = useOwnedNodes();
  return (
    <>
      <output aria-label="owned">{`${owned.status} ${owned.nodes.map((n) => n.name).join(",")}`.trim()}</output>
      <Link to="/auth/link/tok?next=%2Fdata">the sign-in link</Link>
      <Link to="/auth/claim/tok">the claim link</Link>
    </>
  );
}

/** Waits for any sign-out in flight, as a page about to open a session does. */
function Waiter() {
  const { whenSignedOut } = useAuth();
  const [wait, setWait] = useState("not asked");
  const ask = () => {
    setWait("waiting");
    void whenSignedOut().then(() => setWait("settled"));
  };
  return (
    <>
      <button onClick={ask}>Wait for the sign-out</button>
      <output aria-label="wait">{wait}</output>
    </>
  );
}

/** Ada's session replaced by a new one of hers without a page load, as a claim
 *  link mailed to her and redeemed while she is signed in does. */
function Readopt() {
  const { signIn } = useAuth();
  const readopt = () => {
    open(ADA);
    signIn({ ...ADA });
  };
  return <button onClick={readopt}>Adopt a new session for Ada</button>;
}

/** The console as App mounts it, the mailed links' pages beside it. */
function renderConsole(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ThemeProvider>
        <AuthProvider>
          <Identity />
          <Waiter />
          <Readopt />
          <Routes>
            <Route path="/login" element={<h1>Sign in to RETINA</h1>} />
            <Route path="/auth/link/:token" element={<AuthLinkPage />} />
            <Route path="/auth/claim/:token" element={<ClaimPage />} />
            <Route
              path="/*"
              element={
                <RequireAuth isAdmin={false}>
                  <DashboardLayout isAdmin={false}>
                    <Routes>
                      <Route path="data" element={<OwnedPage />} />
                      <Route path="overview" element={<OwnedPage />} />
                      <Route path="map" element={<h1>Map</h1>} />
                    </Routes>
                  </DashboardLayout>
                </RequireAuth>
              }
            />
          </Routes>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

const identity = () => screen.getByLabelText("identity");
const owned = () => screen.getByLabelText("owned");

/** Lets timers fire and the answers they send for land. */
const elapse = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** Opens the account menu and signs out, as a person would. */
async function signOut() {
  fireEvent.keyDown(await screen.findByRole("button", { name: "Account: Ada" }), { key: "Enter" });
  fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
}

/** Ada on the Data Explorer with her nodes listed, and the minute's refresh of
 *  that list out and held under her session. */
async function adaWithListOut() {
  const ada = open(ADA);
  const loc = stubLocation("/data");
  renderConsole(`/data?from=${DAY}&to=${DAY}`);
  await waitFor(() => expect(owned()).toHaveTextContent("ready Roof,Mast"));
  const answerList = holdNextList();
  await elapse(60_000);
  expect(server.holds.has(NODES_PATH)).toBe(false);
  return { loc, untouched: loc.href, answerList, ada };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(`${DAY}T12:00:00Z`));
  stubMatchMedia();
  server.tokens.clear();
  server.cookie = null;
  server.log = [];
  server.holds.clear();
  server.signOut = null;
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => serve(url, init)),
  );
  // usePolling logs every failure.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

describe("a 401 to a request sent under the session before the one adopted since", () => {
  it("keeps the session a sign-in link opened, and its owner's nodes, on an open page", async () => {
    const { loc, untouched, answerList, ada } = await adaWithListOut();
    runOut(ada);

    await signOut();
    await waitFor(() => expect(identity()).toHaveTextContent("nobody"));
    fireEvent.click(screen.getByRole("link", { name: "the sign-in link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(owned()).toHaveTextContent("ready Kite"));
    expect(identity()).toHaveTextContent("pilot@example.com");

    // Ada's refresh reaches the server with her token, which has run out.
    await answerList(401);

    expect(identity()).toHaveTextContent("pilot@example.com");
    expect(owned()).toHaveTextContent(/^ready Kite$/);
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(loc.href).toBe(untouched);
    expect(loc.assign).not.toHaveBeenCalled();
    expect(loc.reload).not.toHaveBeenCalled();

    // Pilot's own list goes on being asked for, and answered.
    const listsBefore = asked(NODES_PATH);
    await elapse(60_000);
    expect(asked(NODES_PATH)).toBe(listsBefore + 1);
    expect(owned()).toHaveTextContent(/^ready Kite$/);

    // While a 401 to a request sent under Pilot's session still ends it.
    runOut(server.cookie);
    await elapse(60_000);
    expect(identity()).toHaveTextContent("nobody");
    expect(owned()).toHaveTextContent(/^signed-out$/);
    expect(loc.href).toBe(untouched);
  });

  it("does not send the session a claim link opened to the sign-in card from a page that needs one", async () => {
    const { loc, answerList, ada } = await adaWithListOut();
    runOut(ada);

    await signOut();
    await waitFor(() => expect(identity()).toHaveTextContent("nobody"));
    fireEvent.click(screen.getByRole("link", { name: "the claim link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, this is my node" }));
    await waitFor(() => expect(identity()).toHaveTextContent("pilot@example.com"));
    // The bound card gives way to the new owner's nodes, a page that needs a
    // session, where any other 401 sends the caller to sign in.
    await elapse(1500);
    await waitFor(() => expect(owned()).toHaveTextContent("ready Kite"));
    loc.pathname = "/overview";
    loc.href = "https://app.retina.fm/overview";

    await answerList(401);

    expect(loc.href).toBe("https://app.retina.fm/overview");
    expect(loc.assign).not.toHaveBeenCalled();
    expect(identity()).toHaveTextContent("pilot@example.com");
    expect(owned()).toHaveTextContent(/^ready Kite$/);
  });

  // Not reachable from a mailed link today, whose pages are not the owner's;
  // held to by construction all the same.
  it("does not mark the same owner's nodes signed out when her session is replaced under the page", async () => {
    const { loc, untouched, answerList, ada } = await adaWithListOut();

    fireEvent.click(screen.getByRole("button", { name: "Adopt a new session for Ada" }));
    runOut(ada);
    await answerList(401);

    expect(identity()).toHaveTextContent("ada@example.com");
    expect(owned()).toHaveTextContent(/^ready Roof,Mast$/);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(loc.href).toBe(untouched);
    // Asked for under the new session, which answers.
    await elapse(60_000);
    expect(owned()).toHaveTextContent(/^ready Roof,Mast$/);
  });
});

describe("an owner route's 401 while this session's sign-out is out", () => {
  it("leaves the sign-out to end the session, once, before a sign-in link waiting on it goes ahead", async () => {
    const { loc, untouched, answerList, ada } = await adaWithListOut();
    runOut(ada);
    const answerSignOut = holdSignOut();
    await signOut();
    await waitFor(() => expect(server.log).toContain("sign-out sent"));

    await answerList(401);

    // Not ended twice: the identity goes when the sign-out answers, together
    // with what that does, and the page no longer lists the session's nodes.
    expect(identity()).toHaveTextContent("ada@example.com");
    expect(screen.getByLabelText("signing out")).toHaveTextContent("true");
    expect(owned()).toHaveTextContent(/^signed-out$/);
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.click(screen.getByRole("link", { name: "the sign-in link" }));
    fireEvent.click(await screen.findByRole("button", { name: "Sign in as pilot@example.com" }));
    await elapse(1000);
    expect(server.log).not.toContain("redemption sent");

    await answerSignOut(() => json(200, { ok: true }));

    await waitFor(() => expect(owned()).toHaveTextContent("ready Kite"));
    expect(identity()).toHaveTextContent("pilot@example.com");
    expect(server.log).toEqual(["sign-out sent", `late 401 ${NODES_PATH}`, "sign-out answered", "redemption sent"]);
    expect(asked("/api/auth/logout")).toBe(1);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(loc.href).toBe(untouched);
  });

  // The 401 is the server's word that the session's token is no good, so no
  // reload can bring the session back: there is nothing for a notice to warn of.
  it.each([
    ["is refused", () => json(500, { detail: "the server's own words" })],
    ["never arrives", () => Promise.reject(new TypeError("Failed to fetch"))],
  ])("says nothing of a sign-out whose answer %s after it", async (_case, reply) => {
    const { loc, untouched, answerList, ada } = await adaWithListOut();
    runOut(ada);
    const answerSignOut = holdSignOut();
    await signOut();
    await waitFor(() => expect(server.log).toContain("sign-out sent"));
    await answerList(401);
    expect(identity()).toHaveTextContent("ada@example.com");

    fireEvent.click(screen.getByRole("button", { name: "Wait for the sign-out" }));
    await elapse(1000);
    expect(screen.getByLabelText("wait")).toHaveTextContent("waiting");

    await answerSignOut(reply as Reply);

    await waitFor(() => expect(screen.getByLabelText("wait")).toHaveTextContent("settled"));
    expect(identity()).toHaveTextContent("nobody");
    expect(screen.getByLabelText("signing out")).toHaveTextContent("false");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(asked("/api/auth/logout")).toBe(1);
    expect(loc.href).toBe(untouched);
    await elapse(10 * 60_000);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(asked("/api/auth/logout")).toBe(1);
  });
});
