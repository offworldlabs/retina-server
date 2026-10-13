import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HttpError } from "@retina/shared";

import { api } from "../api/client";
import ClaimPage from "../pages/ClaimPage";
import { holdSignOut, resetAuth, setAuth, signIn } from "./fakeAuth";

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

vi.mock("../api/client", () => ({
  api: {
    claimPreview: vi.fn(),
    consumeClaim: vi.fn(),
    declineClaim: vi.fn(),
  },
}));

const NODE = "nde4f2k9xq7m3b8";
const UNUSED = "Your link has not been used.";
const MAY_WORK = "Your link may still work.";

/** Where the router is, and how it got there. */
function Where() {
  const { pathname } = useLocation();
  return <output aria-label="location">{`${useNavigationType()} ${pathname}`}</output>;
}

function renderAt(token = "tok") {
  return render(
    <MemoryRouter initialEntries={[`/auth/claim/${token}`]}>
      <Routes>
        <Route path="/auth/claim/:token" element={<ClaimPage />} />
        {/* The new owner's own nodes, not the map the index opens on. */}
        <Route path="/overview" element={<div>dashboard</div>} />
        <Route path="/" element={<div>front door</div>} />
        <Route path="/login" element={<div>login card</div>} />
        <Route path="/map" element={<div>map page</div>} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

/** The claim, opened from the map, with the browser's own Back beside it: the
 *  one way off the card that the card cannot take away. */
function renderFromTheMap(token = "tok") {
  function BrowserBack() {
    const navigate = useNavigate();
    return (
      <button type="button" onClick={() => navigate(-1)}>
        browser back
      </button>
    );
  }
  return render(
    <MemoryRouter initialEntries={["/map", `/auth/claim/${token}`]} initialIndex={1}>
      <BrowserBack />
      <Routes>
        <Route path="/auth/claim/:token" element={<ClaimPage />} />
        <Route path="/overview" element={<div>dashboard</div>} />
        <Route path="/map" element={<div>map page</div>} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

/** Claim link A, with a way to open claim link B in the same page. */
function renderThenAnother() {
  return render(
    <MemoryRouter initialEntries={["/auth/claim/tok-a"]}>
      <Link to="/auth/claim/tok-b">the other link</Link>
      <Routes>
        <Route path="/auth/claim/:token" element={<ClaimPage />} />
        <Route path="/overview" element={<div>dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function location() {
  return screen.getByLabelText("location");
}

const confirmButton = () => screen.getByRole("button", { name: /this is my node/i });
const declineButton = () => screen.getByRole("button", { name: /did not ask/i });

/** Each failure that says nothing about the link, as the request rejects with
 *  it, what the card says about it, and what it can say about the link once a
 *  confirm or a decline met it. A preview never spends the link, and nginx's
 *  429 refuses an answer before the app sees it; any other failed answer may
 *  have reached the app, which spends the token first. */
const FAILURES: [string, unknown, string, string][] = [
  [
    "a rate limit",
    new HttpError(429, "", null),
    "Too many attempts from this network. Wait a minute, then try again.",
    UNUSED,
  ],
  [
    "a deployment that is down",
    new HttpError(503, "", null),
    "This service is unavailable right now. Try again later.",
    MAY_WORK,
  ],
  [
    "a server error",
    new HttpError(500, "", { detail: "boom" }),
    "The server ran into a problem (HTTP 500). Try again in a moment.",
    MAY_WORK,
  ],
  [
    "a dropped connection",
    new TypeError("Failed to fetch"),
    "Could not reach the server. Check your connection, then try again.",
    MAY_WORK,
  ],
  [
    "a server that does not answer",
    new DOMException("No response within 10000 ms", "TimeoutError"),
    "The server did not answer in time. Try again.",
    MAY_WORK,
  ],
];

describe("ClaimPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: NODE });
  });

  it("names the node before either button is pressed", async () => {
    renderAt();

    expect(await screen.findByText(new RegExp(NODE))).toBeInTheDocument();
    expect(confirmButton()).toBeInTheDocument();
    expect(declineButton()).toBeInTheDocument();
  });

  it("does not spend the link to name the node", async () => {
    renderAt();

    await screen.findByText(new RegExp(NODE));
    expect(api.consumeClaim).not.toHaveBeenCalled();
    expect(api.declineClaim).not.toHaveBeenCalled();
  });

  it("binds the node on confirm", async () => {
    vi.mocked(api.consumeClaim).mockResolvedValue({ node_ref: NODE, user: {} });
    renderAt("abc");
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());

    expect(await screen.findByText(/is yours/i)).toBeInTheDocument();
    expect(api.consumeClaim).toHaveBeenCalledWith("abc");
  });

  it("signs the browser in as the account the click created", async () => {
    // The session cookie alone is not enough: the app read its identity once,
    // at load, and would send a fresh owner to the login card.
    const user = { id: "u-1", email: "ada@example.com" };
    vi.mocked(api.consumeClaim).mockResolvedValue({ node_ref: NODE, user });
    renderAt("abc");
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());

    await waitFor(() => expect(signIn).toHaveBeenCalledWith(user));
    expect(await screen.findByText("dashboard", {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it("tells a stranger nothing happened when they decline", async () => {
    vi.mocked(api.declineClaim).mockResolvedValue({ ok: true });
    renderAt("abc");
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(declineButton());

    expect(api.declineClaim).toHaveBeenCalledWith("abc");
    expect(await screen.findByText(/not connected to this address/i)).toBeInTheDocument();
  });

  it("distinguishes a node claimed meanwhile from a link that never worked", async () => {
    vi.mocked(api.consumeClaim).mockRejectedValue(new HttpError(409, "", null));
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());

    expect(await screen.findByText(/already belongs to someone else/i)).toBeInTheDocument();
    expect(screen.queryByText(/no longer valid/i)).not.toBeInTheDocument();
  });

  it("calls the link spent when the confirm meets the server's dead-link answer", async () => {
    vi.mocked(api.consumeClaim).mockRejectedValue(new HttpError(400, "", null));
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());

    expect(await screen.findByText(/no longer valid/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in instead/i })).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it("calls the link spent when the decline meets the server's dead-link answer", async () => {
    vi.mocked(api.declineClaim).mockRejectedValue(new HttpError(400, "", null));
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(declineButton());

    expect(await screen.findByText(/no longer valid/i)).toBeInTheDocument();
  });

  it("offers signing in when the link is spent, rather than a dead end", async () => {
    vi.mocked(api.claimPreview).mockRejectedValue(new HttpError(404, "", null));

    renderAt();

    expect(await screen.findByText(/no longer valid/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in instead/i })).toBeInTheDocument();
  });

  it("offers no answer to the question once the answer is terminal", async () => {
    vi.mocked(api.declineClaim).mockResolvedValue({ ok: true });
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(declineButton());

    // Only the way off the card is left.
    await waitFor(() =>
      expect(screen.getAllByRole("button")).toEqual([screen.getByRole("button", { name: "Back to the map" })]),
    );
  });
});

describe("the ways off the claim card", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: NODE });
  });

  it("signs in through the router, without loading the page again", async () => {
    vi.mocked(api.claimPreview).mockRejectedValue(new HttpError(404, "", null));
    renderAt();

    const link = await screen.findByRole("link", { name: "Sign in instead" });
    expect(link).toHaveAttribute("href", "/login");
    // false: the router cancelled the browser's own navigation, the reload a
    // bare anchor makes, and routed in its place.
    expect(fireEvent.click(link)).toBe(false);

    expect(screen.getByText("login card")).toBeInTheDocument();
    expect(location()).toHaveTextContent("PUSH /login");
  });

  it("offers the way back to the map, as the other sign-in cards do", async () => {
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(screen.getByRole("button", { name: "Back to the map" }));

    expect(screen.getByText("map page")).toBeInTheDocument();
    expect(api.consumeClaim).not.toHaveBeenCalled();
    expect(api.declineClaim).not.toHaveBeenCalled();
  });

  it("offers it while the link is still being checked", () => {
    vi.mocked(api.claimPreview).mockReturnValue(new Promise(() => {}));
    renderAt();

    expect(screen.getByText("Checking the link…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back to the map" })).toBeInTheDocument();
  });
});

describe("a claim still in flight", () => {
  const user = { id: "u-1", email: "ada@example.com" };

  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: NODE });
  });

  // As on the sign-in link's card while it signs in: the request runs whether
  // or not the person stays, and this card is where its outcome is said.
  it.each([
    ["a confirm", () => vi.mocked(api.consumeClaim).mockReturnValue(new Promise(() => {})), confirmButton],
    ["a decline", () => vi.mocked(api.declineClaim).mockReturnValue(new Promise(() => {})), declineButton],
  ])("offers no way off the card while %s is in flight", async (_case, pend, button) => {
    pend();
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(button());

    expect(screen.getByText("One moment…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back to the map" })).not.toBeInTheDocument();
  });

  it("offers it again once the answer is in", async () => {
    vi.mocked(api.consumeClaim).mockRejectedValue(new HttpError(500, "", null));
    renderAt();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back to the map" })).toBeInTheDocument();
  });

  // The browser's Back still leaves. The session the server opened is then not
  // adopted from under whatever page the person went to.
  it("does not sign in when the confirm answers after the person has gone", async () => {
    let answer: (body: { node_ref: string; user: typeof user }) => void = () => {};
    vi.mocked(api.consumeClaim).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    renderFromTheMap();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());
    fireEvent.click(screen.getByRole("button", { name: "browser back" }));
    expect(screen.getByText("map page")).toBeInTheDocument();

    await act(async () => answer({ node_ref: NODE, user }));

    expect(signIn).not.toHaveBeenCalled();
    expect(screen.getByText("map page")).toBeInTheDocument();
    expect(location()).toHaveTextContent("POP /map");
  });

  // A sign-out's answer deletes the session cookie, whichever session holds it
  // by then, so one that landed after the claim would end the session it opens.
  it("does not confirm until a sign-out still in flight has settled", async () => {
    const settleSignOut = holdSignOut();
    vi.mocked(api.consumeClaim).mockResolvedValue({ node_ref: NODE, user });
    renderAt("abc");
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());
    await act(async () => {});

    expect(screen.getByText("One moment…")).toBeInTheDocument();
    expect(api.consumeClaim).not.toHaveBeenCalled();

    await act(async () => settleSignOut());

    expect(api.consumeClaim).toHaveBeenCalledWith("abc");
    expect(await screen.findByText(/is yours/i)).toBeInTheDocument();
    expect(signIn).toHaveBeenCalledWith(user);
  });

  it("sends nothing when the person leaves before a sign-out in flight has settled", async () => {
    const settleSignOut = holdSignOut();
    renderFromTheMap();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(confirmButton());
    fireEvent.click(screen.getByRole("button", { name: "browser back" }));
    await act(async () => settleSignOut());

    expect(api.consumeClaim).not.toHaveBeenCalled();
    expect(signIn).not.toHaveBeenCalled();
    expect(screen.getByText("map page")).toBeInTheDocument();
  });

  it("leaves the page the person went to alone when the decline answers after them", async () => {
    let fail: (err: unknown) => void = () => {};
    vi.mocked(api.declineClaim).mockReturnValue(new Promise((_resolve, reject) => (fail = reject)));
    renderFromTheMap();
    await screen.findByText(new RegExp(NODE));

    fireEvent.click(declineButton());
    fireEvent.click(screen.getByRole("button", { name: "browser back" }));
    await act(async () => fail(new HttpError(500, "", null)));

    expect(screen.getByText("map page")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(location()).toHaveTextContent("POP /map");
  });
});

describe("another claim link opened in the same page", () => {
  const user = { id: "u-1", email: "ada@example.com" };

  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.claimPreview).mockImplementation(async (token: string) => ({ node_ref: `node-${token}` }));
  });

  it("leaves the second link's question alone when the first link's confirm answers late", async () => {
    let answerFirst: (body: { node_ref: string; user: typeof user }) => void = () => {};
    vi.mocked(api.consumeClaim).mockReturnValue(new Promise((resolve) => (answerFirst = resolve)));
    renderThenAnother();
    await screen.findByText(/node-tok-a/);

    fireEvent.click(confirmButton());
    await act(async () => {});
    expect(api.consumeClaim).toHaveBeenCalledWith("tok-a");
    fireEvent.click(screen.getByText("the other link"));
    await screen.findByText(/node-tok-b/);

    await act(async () => answerFirst({ node_ref: "node-tok-a", user }));

    expect(signIn).not.toHaveBeenCalled();
    expect(screen.queryByText(/is yours/i)).not.toBeInTheDocument();
    expect(screen.getByText(/node-tok-b/)).toBeInTheDocument();
    expect(confirmButton()).toBeInTheDocument();
  });

  it("starts the second link from nothing, rather than from the first link's answer", async () => {
    vi.mocked(api.declineClaim).mockResolvedValue({ ok: true });
    renderThenAnother();
    await screen.findByText(/node-tok-a/);
    fireEvent.click(declineButton());
    await screen.findByText(/not connected to this address/i);

    vi.mocked(api.claimPreview).mockReturnValue(new Promise(() => {}));
    fireEvent.click(screen.getByText("the other link"));

    expect(screen.getByText("Checking the link…")).toBeInTheDocument();
    expect(screen.queryByText(/not connected to this address/i)).not.toBeInTheDocument();
  });
});

describe("the redirect after a claim", () => {
  const user = { id: "u-1", email: "ada@example.com" };

  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.useFakeTimers();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: NODE });
    vi.mocked(api.consumeClaim).mockResolvedValue({ node_ref: NODE, user });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Opens the link and confirms it, with every answer let through. */
  async function claimed() {
    const view = renderAt("abc");
    await act(async () => {});
    fireEvent.click(confirmButton());
    await act(async () => {});
    expect(screen.getByText(/is yours/i)).toBeInTheDocument();
    expect(signIn).toHaveBeenCalledWith(user);
    return view;
  }

  it("opens the new owner's nodes after 1200 ms, in place of the claim", async () => {
    await claimed();

    act(() => vi.advanceTimersByTime(1199));
    expect(location()).toHaveTextContent("POP /auth/claim/abc");

    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByText("dashboard")).toBeInTheDocument();
    expect(location()).toHaveTextContent("REPLACE /overview");
  });

  it("leaves nothing pending when the card is unmounted first", async () => {
    const { unmount } = await claimed();
    expect(vi.getTimerCount()).toBe(1);

    act(() => vi.advanceTimersByTime(600));
    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not pull the person off the page they went to instead", async () => {
    await claimed();

    act(() => vi.advanceTimersByTime(600));
    fireEvent.click(screen.getByRole("button", { name: "Back to the map" }));
    expect(screen.getByText("map page")).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);

    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByText("map page")).toBeInTheDocument();
    expect(location()).toHaveTextContent("PUSH /map");
  });
});

describe("a claim link whose request fails", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: NODE });
  });

  async function asked() {
    await screen.findByText(new RegExp(NODE));
  }

  /** What the card says, what it says of the link, and that it does not call
   *  the link dead. */
  async function expectFailure(shown: string, aboutTheLink: string) {
    expect((await screen.findByRole("alert")).textContent).toBe(shown);
    expect(screen.getByText(aboutTheLink)).toBeInTheDocument();
    expect(screen.queryByText(aboutTheLink === UNUSED ? MAY_WORK : UNUSED)).not.toBeInTheDocument();
    expect(screen.queryByText(/no longer valid/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  }

  describe.each(FAILURES)("on %s", (_case, error, shown, afterAnswer) => {
    it("says so when reading the link, and reads it again on a retry", async () => {
      vi.mocked(api.claimPreview).mockRejectedValueOnce(error);
      renderAt();

      // Read, never spent, whatever the failure.
      await expectFailure(shown, UNUSED);
      expect(screen.queryByRole("button", { name: /this is my node/i })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Try again" }));

      await asked();
      expect(api.claimPreview).toHaveBeenCalledTimes(2);
      expect(api.consumeClaim).not.toHaveBeenCalled();
    });

    it("says so when confirming, says what it can of the link, and confirms again on a retry", async () => {
      const user = { id: "u-1", email: "ada@example.com" };
      vi.mocked(api.consumeClaim).mockRejectedValueOnce(error).mockResolvedValueOnce({ node_ref: NODE, user });
      renderAt("abc");
      await asked();

      fireEvent.click(confirmButton());

      await expectFailure(shown, afterAnswer);
      expect(screen.queryByText(/already belongs/i)).not.toBeInTheDocument();
      expect(signIn).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole("button", { name: "Try again" }));

      expect(await screen.findByText(/is yours/i)).toBeInTheDocument();
      expect(api.consumeClaim).toHaveBeenCalledTimes(2);
      expect(api.consumeClaim).toHaveBeenLastCalledWith("abc");
      expect(signIn).toHaveBeenCalledWith(user);
    });

    it("says so when declining, and declines again on a retry", async () => {
      vi.mocked(api.declineClaim).mockRejectedValueOnce(error).mockResolvedValueOnce({ ok: true });
      renderAt("abc");
      await asked();

      fireEvent.click(declineButton());

      await expectFailure(shown, afterAnswer);

      fireEvent.click(screen.getByRole("button", { name: "Try again" }));

      expect(await screen.findByText(/not connected to this address/i)).toBeInTheDocument();
      expect(api.declineClaim).toHaveBeenCalledTimes(2);
      expect(api.consumeClaim).not.toHaveBeenCalled();
    });
  });

  it("calls the link dead once a retry finds the failed confirm had spent it", async () => {
    vi.mocked(api.consumeClaim)
      .mockRejectedValueOnce(new HttpError(500, "", null))
      .mockRejectedValueOnce(new HttpError(400, "", null));
    renderAt();
    await asked();

    fireEvent.click(confirmButton());
    expect(await screen.findByText(MAY_WORK)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(/no longer valid/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in instead" })).toBeInTheDocument();
    expect(api.consumeClaim).toHaveBeenCalledTimes(2);
    expect(signIn).not.toHaveBeenCalled();
  });

  it("offers signing in rather than a retry that would meet the same refusal", async () => {
    vi.mocked(api.consumeClaim).mockRejectedValue(new HttpError(422, "", null));
    renderAt();
    await asked();

    fireEvent.click(confirmButton());

    expect((await screen.findByRole("alert")).textContent).toBe("The server could not accept that request.");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in instead" })).toHaveAttribute("href", "/login");
  });

  it("does the same for a preview the server refuses for good", async () => {
    vi.mocked(api.claimPreview).mockRejectedValue(new HttpError(403, "", null));
    renderAt();

    expect((await screen.findByRole("alert")).textContent).toBe("The server refused the request (HTTP 403).");
    expect(screen.queryByText(/no longer valid/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in instead" })).toBeInTheDocument();
  });
});

describe("a claim link opened by someone already signed in", () => {
  const ADA = { id: "u-1", email: "ada@example.com", name: "Ada" };
  const SIGNED_IN = /^You are signed in as/;

  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: NODE });
  });

  it("offers neither answer until it knows who is here", async () => {
    setAuth({ loading: true });
    renderAt();

    // The preview has answered; who is here has not.
    await waitFor(() => expect(api.claimPreview).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.getByText("Checking the link…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /this is my node/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /did not ask/i })).not.toBeInTheDocument();

    act(() => setAuth({ loading: false, user: ADA }));

    expect(screen.getByText(new RegExp(NODE))).toBeInTheDocument();
    expect(confirmButton()).toBeInTheDocument();
    expect(screen.queryByText("Checking the link…")).not.toBeInTheDocument();
  });

  it("says which account is signed in, and that confirming signs in as the link's", async () => {
    setAuth({ user: ADA });
    renderAt();

    const note = await screen.findByText(SIGNED_IN);
    expect(note.textContent).toBe(
      `You are signed in as ${ADA.email}. Confirming signs this browser in to the account this link was sent to.`,
    );
    expect(confirmButton()).toBeInTheDocument();
    expect(declineButton()).toBeInTheDocument();
  });

  it("says nothing of an account to someone signed out", async () => {
    renderAt();

    await screen.findByText(new RegExp(NODE));
    expect(screen.queryByText(SIGNED_IN)).not.toBeInTheDocument();
    expect(confirmButton()).toBeInTheDocument();
  });
});
