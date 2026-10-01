import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import AuthLinkPage from "../pages/AuthLinkPage";

/**
 * The page the mailed link opens.
 *
 * Redemption is one-shot, so the count of POSTs is part of the behaviour: one
 * made before the person presses anything is one a scanner rendering the page
 * would make too, and the link is dead when its recipient arrives. StrictMode's
 * second effect pass is where an unasked-for request would show, so every case
 * below renders under it.
 */

const signIn = vi.hoisted(() => vi.fn());

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, signIn, logout: vi.fn() }),
}));

const USER = { id: "u-1", email: "pilot@example.com", name: "Pilot" };

type Reply = [status: number, body: unknown];

/** Answers the preview (a GET) and the redemption (a POST). The replies are
 *  the returned object's to change, so a case can have the server recover, and
 *  a reply may be a promise, so a case can hold a request in flight. */
function server(initial: { preview?: Reply; consume?: Reply | Promise<Reply> } = {}) {
  const replies: { preview: Reply; consume: Reply | Promise<Reply> } = {
    preview: [200, { email: USER.email }],
    consume: [200, { user: USER }],
    ...initial,
  };
  const fetchMock = vi.fn(async (_path: string, init?: RequestInit) => {
    const [status, body] = await (init?.method === "POST" ? replies.consume : replies.preview);
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

function openLink(token = "tok-123", { isAdmin = false, search = "" } = {}) {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[`/auth/link/${token}${search}`]}>
        <Routes>
          <Route path="/auth/link/:token" element={<AuthLinkPage isAdmin={isAdmin} />} />
          <Route path="/" element={<div>Dashboard</div>} />
          <Route path="/onboarding" element={<div>My Nodes page</div>} />
        </Routes>
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

describe("the sign-in link page", () => {
  beforeEach(() => {
    signIn.mockClear();
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
      const { fetchMock } = server({ preview: [404, { detail: "That sign-in link is no longer valid" }] });
      openLink();

      expect(await screen.findByText("That sign-in link is no longer valid")).toBeInTheDocument();
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
    server({ consume: [400, { detail: "That sign-in link is no longer valid" }] });
    openLink();
    await pressSignIn();

    expect(await screen.findByText("That sign-in link is no longer valid")).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it("does not call a live link dead when the server could not be reached", async () => {
    // A 404 or 400 is the server's final answer about the token. A 5xx or a
    // dropped connection says nothing about it, and sending the person off to
    // request another throws away a link that still works.
    server({ preview: [503, { detail: "upstream is unwell" }] });
    openLink();

    expect(await screen.findByText(/could not reach the server/i)).toBeInTheDocument();
    expect(screen.queryByText("That sign-in link is no longer valid")).not.toBeInTheDocument();
    expect(screen.getByText(/has not been used/i)).toBeInTheDocument();
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

  it.each([
    ["the preview", { preview: [429, {}] as Reply }, false],
    ["the press", { consume: [429, {}] as Reply }, true],
  ])("says the network is rate limited, not that the server is down, when %s is refused", async (_case, replies, press) => {
    server(replies);
    openLink();
    if (press) await pressSignIn();

    expect(await screen.findByText("Too many sign-in attempts from this network.")).toBeInTheDocument();
    expect(screen.queryByText(/could not reach the server/i)).not.toBeInTheDocument();
    expect(screen.queryByText("That sign-in link is no longer valid")).not.toBeInTheDocument();
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
    ["a dead link", 404, false, "That sign-in link is no longer valid", 1],
    ["a dead link", 404, true, "That sign-in link is no longer valid", 0],
    ["an unreachable server", 503, false, "We could not reach the server to sign you in.", 1],
    ["an unreachable server", 503, true, "We could not reach the server to sign you in.", 0],
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

describe("where the sign-in link ends", () => {
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
