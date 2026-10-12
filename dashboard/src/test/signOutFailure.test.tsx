import { useState } from "react";
import { describe, it, expect, afterEach, beforeEach, vi, type Mock } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";

import Header from "../components/Header";
import RequireAuth from "../components/RequireAuth";
import { AuthProvider, useAuth, type SignOutResult } from "../context/AuthContext";
import { ThemeProvider } from "../context/ThemeContext";
import { describeFailure } from "../utils/requestFailure";
import { stubMatchMedia } from "./matchMedia";

/**
 * A sign-out the server did not confirm.
 *
 * The session cookie is httponly, so only the server can end the session, and
 * a sign-out that timed out or was throttled used to leave the person signed
 * in with nothing said. Now this device drops the identity either way, and a
 * notice says what is left: the session may still be live, and a reload could
 * bring it back. The real AuthProvider, Header and RequireAuth are used, with
 * fetch stubbed, since the question is what the person sees where they land.
 */

const ADA = { id: "u1", name: "Ada", email: "ada@example.com", role: "admin" };

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type SignOutAnswer = (init: RequestInit) => Promise<Response>;

/** Answers each sign-out with the next of `answers`, the last one repeating. */
function stubServer(...answers: SignOutAnswer[]) {
  let sent = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/auth/me") return json(200, ADA);
      if (url === "/api/auth/logout") return answers[Math.min(sent++, answers.length - 1)](init);
      return json(200, {});
    }),
  );
}

const ok: SignOutAnswer = async () => json(200, { ok: true });
const status =
  (code: number): SignOutAnswer =>
  async () =>
    json(code, { detail: "the server's own words" });
/** Holds the answer until the request's own timeout aborts it. */
const hang: SignOutAnswer = (init) =>
  new Promise((_, reject) => init.signal?.addEventListener("abort", () => reject(init.signal?.reason)));

const signOutsSent = () =>
  (fetch as Mock).mock.calls.filter(([url]) => url === "/api/auth/logout").length;

function Where() {
  const { pathname } = useLocation();
  return <output aria-label="location">{`${useNavigationType()} ${pathname}`}</output>;
}

function renderConsole(entry = "/overview", isAdmin = false) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ThemeProvider>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<h1>Sign in to RETINA</h1>} />
            <Route
              path="/*"
              element={
                <RequireAuth isAdmin={isAdmin}>
                  <Header title="Console" isAdmin={isAdmin} />
                  <Routes>
                    <Route path="map" element={<h1>Map</h1>} />
                    <Route path="data" element={<h1>Data</h1>} />
                    <Route path="overview" element={<h1>Overview</h1>} />
                    <Route path="nodes" element={<h1>Nodes</h1>} />
                  </Routes>
                </RequireAuth>
              }
            />
          </Routes>
          <Where />
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

/** Opens the menu by keyboard, as a keyboard user would, and signs out. */
async function signOut() {
  fireEvent.keyDown(await screen.findByRole("button", { name: "Account: Ada" }), { key: "Enter" });
  fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
}

const realLocation = window.location;

function stubLocation() {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    value: { pathname: "/", hostname: "localhost", search: "", href: "http://localhost/", assign },
    writable: true,
    configurable: true,
  });
  return assign;
}

beforeEach(() => {
  stubMatchMedia();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

describe("a sign-out the server confirms", () => {
  it("drops the identity, takes the caller off a private page, and says nothing more", async () => {
    stubServer(ok);
    renderConsole();
    await signOut();

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("REPLACE /map");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(signOutsSent()).toBe(1);
  });

  // The route answers without a session, so a 401 can only mean there was
  // none to end, which is what was asked for.
  it("takes a 401 as already signed out", async () => {
    stubServer(status(401));
    renderConsole();
    await signOut();

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("a sign-out the server does not confirm", () => {
  it.each([
    ["a 500", status(500), { status: 500 }],
    ["nginx's 429", async () => new Response("<html>429 Too Many Requests</html>", { status: 429 }), { status: 429 }],
    ["a 502 from an app that is down", status(502), { status: 502 }],
    ["no connection at all", () => Promise.reject(new TypeError("Failed to fetch")), new TypeError("Failed to fetch")],
  ] as [string, SignOutAnswer, unknown][])(
    "after %s, still signs this device out, and says the session may be live",
    async (_what, answer, error) => {
      stubServer(answer);
      renderConsole();
      await signOut();

      const notice = await screen.findByRole("alert");
      expect(notice).toHaveTextContent("Sign-out not confirmed");
      expect(notice).toHaveTextContent("You are signed out on this device, but the server did not confirm it");
      expect(notice).toHaveTextContent("your session may still be active: reloading the page could sign you back in");
      expect(notice).toHaveTextContent(describeFailure(error).message);
      // Cleared all the same, and landed where a confirmed sign-out lands.
      expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
      expect(screen.getByLabelText("location")).toHaveTextContent("REPLACE /map");
    },
  );

  it("after the request timed out, still signs this device out, and says so", async () => {
    stubServer(hang);
    renderConsole();
    const button = await screen.findByRole("button", { name: "Account: Ada" });

    // Only the clock the request's timeout runs on: React's own scheduling and
    // the test library's polling keep theirs.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    fireEvent.keyDown(button, { key: "Enter" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(screen.getByRole("menuitem", { name: "Signing out…" })).toHaveAttribute("aria-disabled", "true");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    vi.useRealTimers();

    const notice = screen.getByRole("alert");
    expect(notice).toHaveTextContent(describeFailure(new DOMException("", "TimeoutError")).message);
    expect(notice).toHaveTextContent("You are signed out on this device");
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows on the open page the caller stays on", async () => {
    stubServer(status(500));
    renderConsole("/data");
    await signOut();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Data" })).toBeInTheDocument();
  });

  // The admin console has no open pages: RequireAuth sends the caller to the
  // login card, which has no header, so the notice cannot belong to a page.
  it("shows over the admin console's login card", async () => {
    stubServer(status(503));
    renderConsole("/nodes", true);
    await signOut();

    expect(await screen.findByRole("heading", { name: "Sign in to RETINA" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(describeFailure({ status: 503 }).message);
  });

  // The Sign out item that had focus went with the menu.
  it("gives focus to Try again, rather than leave it on the page", async () => {
    stubServer(status(500));
    renderConsole();
    await signOut();

    expect(await screen.findByRole("button", { name: "Try again" })).toHaveFocus();
  });
});

describe("the notice's Try again", () => {
  it("sends the sign-out again, and goes once it is confirmed", async () => {
    stubServer(status(500), ok);
    renderConsole();
    await signOut();

    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(signOutsSent()).toBe(2);
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("follows an Access sign-out if the server names one this time", async () => {
    const assign = stubLocation();
    stubServer(status(500), async () => json(200, { ok: true, redirect: "/cdn-cgi/access/logout" }));
    renderConsole();
    await signOut();

    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/cdn-cgi/access/logout"));
  });

  // A screen reader announces an alert again only when its words change.
  it("says so when the retry is not confirmed either, with the new reason", async () => {
    stubServer(status(500), status(429));
    renderConsole();
    await signOut();

    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("still has not confirmed it"));
    expect(screen.getByRole("alert")).toHaveTextContent(describeFailure({ status: 429 }).message);
    expect(signOutsSent()).toBe(2);
  });

  it("shows the retry in flight, and sends it once however often it is pressed", async () => {
    let answer: (r: Response) => void = () => {};
    stubServer(status(500), () => new Promise((resolve) => (answer = resolve)));
    renderConsole();
    await signOut();

    const retry = await screen.findByRole("button", { name: "Try again" });
    fireEvent.click(retry);
    fireEvent.click(retry);
    expect(retry).toHaveTextContent("Trying again…");
    expect(retry).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(retry);
    expect(signOutsSent()).toBe(2);

    answer(json(200, { ok: true }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});

describe("dismissing the notice", () => {
  it("removes it without sending anything", async () => {
    stubServer(status(500));
    renderConsole();
    await signOut();

    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(signOutsSent()).toBe(1);
  });
});

function Probe() {
  const { user, logout, signIn } = useAuth();
  const [results, setResults] = useState<SignOutResult[]>([]);
  return (
    <>
      <output aria-label="identity">{user ? user.name : "nobody"}</output>
      <output aria-label="results">{JSON.stringify(results)}</output>
      <button
        onClick={() => {
          // Twice in one go, as a double click lands before a re-render.
          void Promise.all([logout(), logout()]).then(setResults);
        }}
      >
        Sign out twice
      </button>
      <button onClick={() => signIn(ADA)}>Sign in again</button>
    </>
  );
}

function renderProbe() {
  return render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
}

describe("the provider's sign-out", () => {
  it("sends one request at a time, and answers both callers with its result", async () => {
    let answer: (r: Response) => void = () => {};
    stubServer(() => new Promise((resolve) => (answer = resolve)));
    renderProbe();
    await waitFor(() => expect(screen.getByLabelText("identity")).toHaveTextContent("Ada"));

    fireEvent.click(screen.getByRole("button", { name: "Sign out twice" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign out twice" }));
    expect(signOutsSent()).toBe(1);

    answer(json(500, null));
    await waitFor(() => expect(screen.getByLabelText("identity")).toHaveTextContent("nobody"));
    const results: SignOutResult[] = JSON.parse(screen.getByLabelText("results").textContent ?? "[]");
    expect(results).toHaveLength(2);
    expect(results[0]).toEqual(results[1]);
    expect(results[0]).toMatchObject({ redirected: false, failure: { kind: "server", status: 500 } });
  });

  it("sends a new one once the last has settled", async () => {
    stubServer(status(500));
    renderProbe();
    await waitFor(() => expect(screen.getByLabelText("identity")).toHaveTextContent("Ada"));

    fireEvent.click(screen.getByRole("button", { name: "Sign out twice" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Sign out twice" }));
    await waitFor(() => expect(signOutsSent()).toBe(2));
  });

  // A new session settles whatever the last sign-out left unconfirmed.
  it("drops the notice when someone signs in again", async () => {
    stubServer(status(500));
    renderProbe();
    await waitFor(() => expect(screen.getByLabelText("identity")).toHaveTextContent("Ada"));
    fireEvent.click(screen.getByRole("button", { name: "Sign out twice" }));
    await screen.findByRole("alert");

    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("identity")).toHaveTextContent("Ada");
  });
});

describe("a page restored from the back-forward cache mid-sign-out", () => {
  // After an Access sign-out the page leaves for the edge with Sign out still
  // pending; Back can restore it exactly so, and it must be usable again.
  it("offers Sign out again", async () => {
    stubLocation();
    stubServer(async () => json(200, { ok: true, redirect: "/cdn-cgi/access/logout" }));
    renderConsole();
    await signOut();
    expect(await screen.findByRole("menuitem", { name: "Signing out…" })).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(Object.assign(new Event("pageshow"), { persisted: true }));
    });

    expect(screen.getByRole("menuitem", { name: "Sign out" })).not.toHaveAttribute("aria-disabled");
  });
});

/** Read through the hook, so a field that is not on its type fails to compile
 *  here under `npm run typecheck`. */
function Fields() {
  const auth = useAuth();
  // @ts-expect-error useAuth() is AuthContextValue; were it `any`, this would compile and the directive would fail
  const unknownField: unknown = auth.noSuchField;
  // @ts-expect-error the console's user carries no avatar since the provider login was retired
  const avatar: unknown = auth.user?.avatar;
  return <output aria-label="fields">{`${typeof unknownField} ${typeof avatar}`}</output>;
}

describe("useAuth", () => {
  it("is typed, so a field it does not carry is a compile error", async () => {
    stubServer(ok);
    render(
      <AuthProvider>
        <Fields />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByLabelText("fields")).toHaveTextContent("undefined undefined"));
  });
});
