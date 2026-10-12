import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";
import AuthLinkPage from "../pages/AuthLinkPage";
import { resetAuth, setAuth, signIn } from "./fakeAuth";

/**
 * The page the mailed link opens.
 *
 * Redemption is one-shot, so the count of POSTs is part of the behaviour: one
 * made before the person presses anything is one a scanner rendering the page
 * would make too, and the link is dead when its recipient arrives. StrictMode's
 * second effect pass is where an unasked-for request would show, so every case
 * below renders under it.
 */

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

const USER = { id: "u-1", email: "pilot@example.com", name: "Pilot" };
const OTHER = { id: "u-2", email: "ada@example.com", name: "Ada" };

const DEAD_LINK = "That sign-in link is no longer valid";
const UNUSED = "Your link has not been used.";
const MAY_WORK = "Your link may still work.";

type Reply = [status: number, body: unknown];
/** A reply, a reply still on its way, a failure to connect, or no answer at all
 *  ("silence"), which ends when the shared request's own timer gives up. */
type Answer = Reply | Promise<Reply> | Error | "silence";

/** Answers the preview (a GET) and the redemption (a POST). The answers are
 *  the returned object's to change, so a case can have the server recover. */
function server(initial: { preview?: Answer; consume?: Answer } = {}) {
  const replies: { preview: Answer; consume: Answer } = {
    preview: [200, { email: USER.email }],
    consume: [200, { user: USER }],
    ...initial,
  };
  const fetchMock = vi.fn(async (_path: string, init?: RequestInit) => {
    const answer = init?.method === "POST" ? replies.consume : replies.preview;
    if (answer === "silence") {
      // As fetch does once its signal is aborted: it rejects with the reason.
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal.reason));
      });
    }
    if (answer instanceof Error) throw answer;
    const [status, body] = await answer;
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, replies };
}

function calls(fetchMock: Mock, method: "GET" | "POST") {
  return fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
}

/** Where the router is, how it got there, and the page it was asked to carry. */
function Where() {
  const { pathname, state } = useLocation();
  return (
    <output aria-label="location" data-next={state?.next ?? ""}>
      {`${useNavigationType()} ${pathname}`}
    </output>
  );
}

function openLink(token = "tok-123", { isAdmin = false, search = "" } = {}) {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[`/auth/link/${token}${search}`]}>
        <Routes>
          <Route path="/auth/link/:token" element={<AuthLinkPage isAdmin={isAdmin} />} />
          <Route path="/" element={<div>Dashboard</div>} />
          <Route path="/onboarding" element={<div>My Nodes page</div>} />
          <Route path="/login" element={<div>Login card</div>} />
        </Routes>
        <Where />
      </MemoryRouter>
    </StrictMode>
  );
}

/** Link A, with a way to open link B in the same page. */
function openLinkThenAnother() {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={["/auth/link/tok-a"]}>
        <Link to="/auth/link/tok-b">the other link</Link>
        <Routes>
          <Route path="/auth/link/:token" element={<AuthLinkPage />} />
          <Route path="/" element={<div>Dashboard</div>} />
        </Routes>
      </MemoryRouter>
    </StrictMode>
  );
}

async function pressSignIn() {
  (await screen.findByRole("button", { name: "Sign in" })).click();
}

/** Lets every settled promise run its continuations. */
function settle() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function location() {
  return screen.getByLabelText("location");
}

describe("the sign-in link page", () => {
  beforeEach(() => {
    resetAuth();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("names the address and spends nothing until the person presses sign in", async () => {
    const { fetchMock } = server();
    openLink("tok-abc");

    expect(await screen.findByText(USER.email)).toBeInTheDocument();
    expect(calls(fetchMock, "GET").map(([path]) => path)).toContain("/api/auth/magic-link/tok-abc");
    expect(calls(fetchMock, "POST")).toHaveLength(0);
    expect(signIn).not.toHaveBeenCalled();
  });

  it("redeems the token exactly once when pressed", async () => {
    const { fetchMock } = server();
    openLink("tok-abc");
    await pressSignIn();

    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    const posts = calls(fetchMock, "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe("/api/auth/magic-link/consume");
    expect(JSON.parse(posts[0][1].body)).toEqual({ token: "tok-abc" });
  });

  it("adopts the identity the redemption answered with, then routes to the app", async () => {
    server();
    openLink();
    await pressSignIn();

    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    expect(signIn).toHaveBeenCalledWith(USER);
  });

  it("calls a link dead before anything is pressed when the preview does not know it", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { fetchMock } = server({ preview: [404, { detail: DEAD_LINK }] });
      openLink();

      expect(await screen.findByText(DEAD_LINK)).toBeInTheDocument();
      // The login card, so the failure ends somewhere they can ask for another.
      expect(screen.getByText("Retina")).toBeInTheDocument();
      expect(screen.getByLabelText("Email address")).toBeInTheDocument();
      expect(calls(fetchMock, "POST")).toHaveLength(0);
      // An old link is the page's commonest visitor, not an error.
      expect(logged).not.toHaveBeenCalled();
    } finally {
      logged.mockRestore();
    }
  });

  it("calls a link dead when it was spent between the preview and the press", async () => {
    server({ consume: [400, { detail: DEAD_LINK }] });
    openLink();
    await pressSignIn();

    expect(await screen.findByText(DEAD_LINK)).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it("does not call a live link dead when the server is unavailable", async () => {
    // A 404 or 400 is the server's final answer about the token. A 5xx or a
    // dropped connection says nothing about it, and sending the person off to
    // request another throws away a link that still works.
    server({ preview: [503, { detail: "upstream is unwell" }] });
    openLink();

    expect(await screen.findByRole("alert")).toHaveTextContent("This service is unavailable right now. Try again later.");
    expect(screen.queryByText(DEAD_LINK)).not.toBeInTheDocument();
    expect(screen.getByText(UNUSED)).toBeInTheDocument();
    expect(screen.queryByText(/upstream is unwell/)).not.toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it("asks the question again once a failed preview is retried", async () => {
    const { fetchMock, replies } = server({ preview: [500, {}] });
    openLink();

    const retry = await screen.findByRole("button", { name: /try again/i });
    replies.preview = [200, { email: USER.email }];
    retry.click();

    expect(await screen.findByText(USER.email)).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(0);
  });

  it("retries the redemption itself once a failed press is retried", async () => {
    // The person already said yes, so the retry is that press, not the question.
    const { fetchMock, replies } = server({ consume: [500, {}] });
    openLink("tok-retry");
    await pressSignIn();

    const retry = await screen.findByRole("button", { name: /try again/i });
    replies.consume = [200, { user: USER }];
    retry.click();

    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    const posts = calls(fetchMock, "POST");
    expect(posts).toHaveLength(2);
    expect(JSON.parse(posts[1][1].body)).toEqual({ token: "tok-retry" });
  });

  it("calls the link dead once a retry finds the failed redemption had spent it", async () => {
    // The app spends the token before it opens the session, so a 500 can come
    // after the link is gone. The card said only that it may still work.
    const { fetchMock, replies } = server({ consume: [500, {}] });
    openLink();
    await pressSignIn();

    expect(await screen.findByText(MAY_WORK)).toBeInTheDocument();
    replies.consume = [400, { detail: DEAD_LINK }];
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(DEAD_LINK)).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(2);
    expect(signIn).not.toHaveBeenCalled();
  });

  it("starts afresh when another link opens in the same page", async () => {
    const { fetchMock, replies } = server();
    openLinkThenAnother();
    await screen.findByText(USER.email);

    replies.preview = [503, {}];
    screen.getByText("the other link").click();
    const retry = await screen.findByRole("button", { name: /try again/i });
    replies.preview = [200, { email: "other@example.com" }];
    retry.click();

    // The second link's own question, not a redemption of it made on the
    // strength of the first link's address.
    expect(await screen.findByText("other@example.com")).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(0);
  });

  it("leaves the second link's question alone when the first link's redemption answers late", async () => {
    let answerFirst: (reply: Reply) => void = () => {};
    const { replies } = server({ consume: new Promise<Reply>((resolve) => (answerFirst = resolve)) });
    openLinkThenAnother();
    await pressSignIn();

    replies.preview = [200, { email: "other@example.com" }];
    screen.getByText("the other link").click();
    await screen.findByText("other@example.com");

    answerFirst([200, { user: USER }]);
    await settle();

    expect(signIn).not.toHaveBeenCalled();
    expect(screen.queryByText("Dashboard")).not.toBeInTheDocument();
    expect(screen.getByText("other@example.com")).toBeInTheDocument();
  });

  it.each([
    ["a dead link", 404, false, DEAD_LINK, 1],
    ["a dead link", 404, true, DEAD_LINK, 0],
    ["an unavailable server", 503, false, "This service is unavailable right now. Try again later.", 1],
    ["an unavailable server", 503, true, "This service is unavailable right now. Try again later.", 0],
  ])("offers the map after %s unless on the admin console (%i, admin: %s)", async (_case, status, isAdmin, shown, offered) => {
    server({ preview: [status, { detail: "whatever the server said" }] });
    openLink("tok-123", { isAdmin });

    await screen.findByText(shown);
    expect(screen.queryAllByRole("button", { name: "Back to the map" })).toHaveLength(offered);
  });

  it.each([
    [false, 1],
    [true, 0],
  ])("offers the map while asking unless on the admin console (admin: %s)", async (isAdmin, offered) => {
    server();
    openLink("tok-123", { isAdmin });

    await screen.findByRole("button", { name: "Sign in" });
    expect(screen.queryAllByRole("button", { name: "Back to the map" })).toHaveLength(offered);
  });
});

/** Each failure that says nothing about the token, with what the card says,
 *  and what it can say about the link once a redemption met it. A preview never
 *  spends the link, and nginx's 429 refuses a redemption before the app sees
 *  it; any other failed redemption may have reached the app, which spends the
 *  token before it opens the session. */
const FAILURES: [string, Answer, string, string][] = [
  ["a rate limit", [429, {}], "Too many attempts from this network. Wait a minute, then try again.", UNUSED],
  ["a deployment that is down", [503, {}], "This service is unavailable right now. Try again later.", MAY_WORK],
  ["a server error", [500, {}], "The server ran into a problem (HTTP 500). Try again in a moment.", MAY_WORK],
  [
    "a dropped connection",
    new TypeError("Failed to fetch"),
    "Could not reach the server. Check your connection, then try again.",
    MAY_WORK,
  ],
  ["a server that does not answer", "silence", "The server did not answer in time. Try again.", MAY_WORK],
];

describe.each(FAILURES)("a link whose request meets %s", (_case, failing, shown, afterRedemption) => {
  let quiet: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    resetAuth();
    // The page logs each of these, as useFetch would have.
    quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    // The shared request's own ten-second timer, run on a clock the test can
    // move; it still ticks in real time, so the queries below can wait.
    if (failing === "silence") vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    quiet.mockRestore();
    vi.unstubAllGlobals();
  });

  async function failure() {
    if (failing === "silence") {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
    }
    return screen.findByRole("alert");
  }

  /** The card a failure leaves: what happened, what can be said of the link,
   *  and a way to try again. */
  function expectFailureCard(aboutTheLink: string) {
    expect(screen.getByRole("alert").textContent).toBe(shown);
    expect(screen.getByText(aboutTheLink)).toBeInTheDocument();
    expect(screen.queryByText(aboutTheLink === UNUSED ? MAY_WORK : UNUSED)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText(DEAD_LINK)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  }

  it("says so when it reads the link, and asks again once retried", async () => {
    const { fetchMock, replies } = server({ preview: failing });
    openLink();
    await failure();

    // Read, never spent, whatever the failure.
    expectFailureCard(UNUSED);
    expect(calls(fetchMock, "POST")).toHaveLength(0);

    replies.preview = [200, { email: USER.email }];
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText(USER.email)).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(0);
  });

  it("says so when it redeems the link, and redeems it once retried", async () => {
    const { fetchMock, replies } = server({ consume: failing });
    openLink("tok-retry");
    await pressSignIn();
    await failure();

    expectFailureCard(afterRedemption);

    replies.consume = [200, { user: USER }];
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Dashboard")).toBeInTheDocument();
    expect(signIn).toHaveBeenCalledWith(USER);
    const posts = calls(fetchMock, "POST");
    expect(posts).toHaveLength(2);
    expect(JSON.parse(posts[1][1].body)).toEqual({ token: "tok-retry" });
  });
});

describe("a link whose request is refused for good", () => {
  let quiet: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    resetAuth();
    quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    quiet.mockRestore();
    vi.unstubAllGlobals();
  });

  it("says why and offers the login card rather than a retry that would meet the same answer", async () => {
    server({ preview: [403, { detail: "blocked" }] });
    openLink("tok-123", { search: "?next=/onboarding" });

    expect((await screen.findByRole("alert")).textContent).toBe("The server refused the request (HTTP 403).");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.queryByText(DEAD_LINK)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "Go to sign in" }));
    expect(screen.getByText("Login card")).toBeInTheDocument();
    // A new link asked for there opens the page this one was going to.
    expect(location()).toHaveAttribute("data-next", "/onboarding");
  });

  it("does the same for a refused redemption", async () => {
    const { fetchMock } = server({ consume: [422, {}] });
    openLink();
    await pressSignIn();

    expect((await screen.findByRole("alert")).textContent).toBe("The server could not accept that request.");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to sign in" })).toHaveAttribute("href", "/login");
    expect(calls(fetchMock, "POST")).toHaveLength(1);
    expect(signIn).not.toHaveBeenCalled();
  });
});

describe("a link opened by someone already signed in", () => {
  beforeEach(() => {
    resetAuth();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks before replacing another account, naming both", async () => {
    const { fetchMock } = server();
    setAuth({ user: OTHER });
    openLink();

    const question = await screen.findByText(/^You are signed in as/);
    expect(question.textContent).toBe(`You are signed in as ${OTHER.email}. This link signs in as ${USER.email}.`);
    expect(screen.getByRole("button", { name: `Sign in as ${USER.email}` })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: `Stay signed in as ${OTHER.email}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(0);
  });

  it.each([
    ["", "Dashboard", "/"],
    ["?next=/onboarding", "My Nodes page", "/onboarding"],
  ])("leaves the session and the link alone when the person stays (%s)", async (search, page, path) => {
    const { fetchMock } = server();
    setAuth({ user: OTHER });
    openLink("tok-123", { search });

    fireEvent.click(await screen.findByRole("link", { name: `Stay signed in as ${OTHER.email}` }));

    expect(screen.getByText(page)).toBeInTheDocument();
    expect(location()).toHaveTextContent(`REPLACE ${path}`);
    expect(calls(fetchMock, "POST")).toHaveLength(0);
    expect(signIn).not.toHaveBeenCalled();
  });

  it("switches accounts when the person chooses the link's", async () => {
    const { fetchMock } = server();
    setAuth({ user: OTHER });
    openLink("tok-switch", { search: "?next=/onboarding" });

    fireEvent.click(await screen.findByRole("button", { name: `Sign in as ${USER.email}` }));

    expect(await screen.findByText("My Nodes page")).toBeInTheDocument();
    expect(location()).toHaveTextContent("REPLACE /onboarding");
    const posts = calls(fetchMock, "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0][1].body)).toEqual({ token: "tok-switch" });
    expect(signIn).toHaveBeenCalledTimes(1);
    expect(signIn).toHaveBeenCalledWith(USER);
  });

  it("says so when the link died before the switch, and keeps the account it had", async () => {
    server({ consume: [400, { detail: DEAD_LINK }] });
    setAuth({ user: OTHER });
    openLink();

    fireEvent.click(await screen.findByRole("button", { name: `Sign in as ${USER.email}` }));

    expect(await screen.findByText(DEAD_LINK)).toBeInTheDocument();
    expect(screen.getByText(/^You are still signed in as/).textContent).toBe(`You are still signed in as ${OTHER.email}.`);
    expect(signIn).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("link", { name: "Continue" }));
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("says so when the link is dead on arrival, rather than leaving without a word", async () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { fetchMock } = server({ preview: [404, { detail: DEAD_LINK }] });
      setAuth({ user: OTHER });
      openLink("tok-123", { search: "?next=/onboarding" });

      expect(await screen.findByText(DEAD_LINK)).toBeInTheDocument();
      expect(screen.getByText(/^You are still signed in as/).textContent).toBe(`You are still signed in as ${OTHER.email}.`);
      fireEvent.click(screen.getByRole("link", { name: "Continue" }));
      expect(screen.getByText("My Nodes page")).toBeInTheDocument();
      expect(calls(fetchMock, "POST")).toHaveLength(0);
      expect(quiet).not.toHaveBeenCalled();
    } finally {
      quiet.mockRestore();
    }
  });

  it.each([
    ["the same address", USER.email],
    ["the address in other letters", "Pilot@Example.COM"],
    ["the address with stray spaces", ` ${USER.email} `],
  ])("spends nothing when the session is already %s", async (_case, signedInAs) => {
    const { fetchMock } = server();
    setAuth({ user: { ...USER, email: signedInAs } });
    openLink("tok-123", { search: "?next=/onboarding" });

    const already = await screen.findByText(/^You are already signed in as/);
    expect(already.textContent).toBe(`You are already signed in as ${signedInAs}.`);
    expect(screen.queryByRole("button", { name: /^Sign in/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "Continue" }));

    expect(screen.getByText("My Nodes page")).toBeInTheDocument();
    expect(location()).toHaveTextContent("REPLACE /onboarding");
    expect(calls(fetchMock, "POST")).toHaveLength(0);
    expect(signIn).not.toHaveBeenCalled();
  });

  it.each([
    ["someone else", OTHER, /^You are signed in as/],
    ["nobody", null, /^Sign in as/],
  ])("decides nothing until it knows who is here (%s)", async (_case, settledAs, question) => {
    const { fetchMock } = server();
    setAuth({ loading: true });
    openLink();

    // The preview has answered; who is here has not.
    await waitFor(() => expect(calls(fetchMock, "GET").length).toBeGreaterThan(0));
    await act(settle);
    expect(screen.getByText("Checking your link…")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();

    act(() => setAuth({ loading: false, user: settledAs }));

    expect(screen.getByText(question)).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(0);
    expect(signIn).not.toHaveBeenCalled();
  });
});

describe("where the sign-in link ends", () => {
  beforeEach(() => {
    resetAuth();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function signInFrom(search: string, { isAdmin = false } = {}) {
    const { fetchMock } = server();
    openLink("tok-123", { isAdmin, search });
    await pressSignIn();
    return fetchMock;
  }

  it("opens the page the visitor asked for before signing in", async () => {
    const fetchMock = await signInFrom("?next=/onboarding");
    expect(await screen.findByText("My Nodes page")).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(1);
  });

  // The mailed URL is anyone's to edit, whatever the server put in it.
  it.each(["?next=//evil.example.com", "?next=https://evil.example.com/", ""])(
    "opens the console's root for anything else (%s)",
    async (search) => {
      await signInFrom(search);
      expect(await screen.findByText("Dashboard")).toBeInTheDocument();
    }
  );

  it("opens the root on the admin console whatever the link carries", async () => {
    await signInFrom("?next=/onboarding", { isAdmin: true });
    expect(await screen.findByText("Dashboard")).toBeInTheDocument();
  });
});
