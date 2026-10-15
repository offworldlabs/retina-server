import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HttpError, request } from "@retina/shared";

import { api } from "../api/client";
import EmailChangePage from "../pages/EmailChangePage";
import { resetAuth, setAuth } from "./fakeAuth";

const { updateUser } = vi.hoisted(() => ({ updateUser: vi.fn() }));

vi.mock("../context/AuthContext", async () => {
  const { useFakeAuth } = await import("./fakeAuth");
  return { useAuth: () => ({ ...useFakeAuth(), updateUser }) };
});

vi.mock("@retina/shared", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@retina/shared")>()),
  request: vi.fn(),
}));

vi.mock("../api/client", () => ({
  api: {
    emailChangePreview: vi.fn(),
    consumeEmailChange: vi.fn(),
  },
}));

const NEW = "new@example.com";
const UNUSED = "Your link has not been used.";
const MAY_WORK = "Your link may still work.";
const DEAD = "That link is no longer valid. Links last fifteen minutes and work once.";
const ADA = { id: "u-ada", email: "ada@example.com", name: "Ada" };
const ADA_MOVED = { ...ADA, email: NEW };

/** Where the router is. */
function Where() {
  return <output aria-label="location">{useLocation().pathname}</output>;
}

function renderAt(token = "tok") {
  return render(
    <MemoryRouter initialEntries={[`/auth/email/${token}`]}>
      <Routes>
        <Route path="/auth/email/:token" element={<EmailChangePage />} />
        <Route path="/account" element={<div>account page</div>} />
        <Route path="/login" element={<div>login card</div>} />
        <Route path="/map" element={<div>map page</div>} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

/** The link, opened from the map, with the browser's own Back beside it. */
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
    <MemoryRouter initialEntries={["/map", `/auth/email/${token}`]} initialIndex={1}>
      <BrowserBack />
      <Routes>
        <Route path="/auth/email/:token" element={<EmailChangePage />} />
        <Route path="/map" element={<div>map page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const confirmButton = () => screen.getByRole("button", { name: "Yes, use this address" });

/** Opens the link and presses the button. */
async function confirmed(token = "tok") {
  const view = renderAt(token);
  await screen.findByText(NEW);
  fireEvent.click(confirmButton());
  return view;
}

/** Each failure that says nothing about the link, as the request rejects with
 *  it, what the card says about it, and what it can say about the link once
 *  the confirm met it. A preview never spends the link, and nginx's 429
 *  refuses an answer before the app sees it. */
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
    "a dropped connection",
    new TypeError("Failed to fetch"),
    "Could not reach the server. Check your connection, then try again.",
    MAY_WORK,
  ],
];

describe("EmailChangePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.emailChangePreview).mockResolvedValue({ email: NEW });
    vi.mocked(api.consumeEmailChange).mockResolvedValue({ email: NEW });
    vi.mocked(request).mockResolvedValue(ADA_MOVED);
  });

  it("names the new address and asks before it changes anything", async () => {
    renderAt("abc");

    const question = await screen.findByText((_, el) => el?.textContent === `Use ${NEW} to sign in to RETINA?`);
    expect(question).toBeInTheDocument();
    expect(api.emailChangePreview).toHaveBeenCalledWith("abc");
    expect(screen.getAllByRole("button", { name: /^Yes/ })).toHaveLength(1);
  });

  it("consumes nothing until the button is pressed", async () => {
    renderAt();

    await screen.findByText(NEW);
    await act(async () => {});

    expect(api.consumeEmailChange).not.toHaveBeenCalled();
    expect(request).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("says the sign-in address is now the new one, and continues to the sign-in card when signed out", async () => {
    await confirmed("abc");

    expect(
      await screen.findByText((_, el) => el?.textContent === `Done. Your sign-in address is now ${NEW}.`),
    ).toBeInTheDocument();
    expect(api.consumeEmailChange).toHaveBeenCalledTimes(1);
    expect(api.consumeEmailChange).toHaveBeenCalledWith("abc");
    // No session to refresh.
    expect(request).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();

    const next = screen.getByRole("link", { name: "Continue" });
    expect(next).toHaveAttribute("href", "/login");
    fireEvent.click(next);
    expect(screen.getByText("login card")).toBeInTheDocument();
  });

  it("refreshes the signed-in user and continues to the account page", async () => {
    setAuth({ user: ADA });
    await confirmed();

    expect(await screen.findByText(/Your sign-in address is now/)).toBeInTheDocument();
    await waitFor(() => expect(updateUser).toHaveBeenCalledWith(ADA_MOVED));
    expect(request).toHaveBeenCalledTimes(1);
    expect(vi.mocked(request).mock.calls[0][0]).toBe("/api/auth/me");

    const next = screen.getByRole("link", { name: "Continue" });
    expect(next).toHaveAttribute("href", "/account");
    fireEvent.click(next);
    expect(screen.getByText("account page")).toBeInTheDocument();
  });

  it("keeps the change when the user cannot be refreshed", async () => {
    setAuth({ user: ADA });
    vi.mocked(request).mockRejectedValue(new HttpError(500, "", null));
    await confirmed();

    expect(await screen.findByText(/Your sign-in address is now/)).toBeInTheDocument();
    await waitFor(() => expect(request).toHaveBeenCalled());
    await act(async () => {});
    expect(updateUser).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("still refreshes the user when the confirm answers after the person has left", async () => {
    setAuth({ user: ADA });
    let answer: (body: { email: string }) => void = () => {};
    vi.mocked(api.consumeEmailChange).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    renderFromTheMap();
    await screen.findByText(NEW);

    fireEvent.click(confirmButton());
    fireEvent.click(screen.getByRole("button", { name: "browser back" }));
    expect(screen.getByText("map page")).toBeInTheDocument();
    await act(async () => answer({ email: NEW }));

    await waitFor(() => expect(updateUser).toHaveBeenCalledWith(ADA_MOVED));
    expect(screen.getByText("map page")).toBeInTheDocument();
  });

  it("holds the button until it knows who is signed in", async () => {
    setAuth({ loading: true });
    renderAt();

    await waitFor(() => expect(api.emailChangePreview).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.getByText("Checking the link…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Yes/ })).not.toBeInTheDocument();

    act(() => setAuth({ loading: false, user: ADA }));

    expect(confirmButton()).toBeInTheDocument();
    expect(screen.queryByText("Checking the link…")).not.toBeInTheDocument();
  });

  it("offers no way off the card while the confirm is in flight", async () => {
    vi.mocked(api.consumeEmailChange).mockReturnValue(new Promise(() => {}));
    await confirmed();

    expect(screen.getByText("One moment…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back to the map" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Yes/ })).not.toBeInTheDocument();
  });
});

describe("a link that is no longer valid", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.emailChangePreview).mockResolvedValue({ email: NEW });
    vi.mocked(request).mockResolvedValue(ADA_MOVED);
  });

  it("says so on the preview, and offers sign-in when signed out", async () => {
    vi.mocked(api.emailChangePreview).mockRejectedValue(new HttpError(404, "", null));
    renderAt();

    expect(await screen.findByText(DEAD)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Yes/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in instead" })).toHaveAttribute("href", "/login");
    expect(api.consumeEmailChange).not.toHaveBeenCalled();
  });

  it("says so on the preview, and offers the account page when signed in", async () => {
    setAuth({ user: ADA });
    vi.mocked(api.emailChangePreview).mockRejectedValue(new HttpError(404, "", null));
    renderAt();

    expect(await screen.findByText(DEAD)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to your account" })).toHaveAttribute("href", "/account");
  });

  it("says so when the confirm meets the server's dead-link answer", async () => {
    vi.mocked(api.consumeEmailChange).mockRejectedValue(new HttpError(400, "", null));
    await confirmed();

    expect(await screen.findByText(DEAD)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in instead" })).toBeInTheDocument();
    expect(screen.queryByText(/Your sign-in address is now/)).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("says the address now belongs to another account when it was taken meanwhile", async () => {
    setAuth({ user: ADA });
    vi.mocked(api.consumeEmailChange).mockRejectedValue(new HttpError(409, "", null));
    await confirmed();

    expect(await screen.findByText("That address now belongs to another account.")).toBeInTheDocument();
    expect(screen.queryByText(/no longer valid/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to your account" })).toHaveAttribute("href", "/account");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
    expect(updateUser).not.toHaveBeenCalled();
  });
});

describe("a link whose request fails", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.emailChangePreview).mockResolvedValue({ email: NEW });
    vi.mocked(api.consumeEmailChange).mockResolvedValue({ email: NEW });
    vi.mocked(request).mockResolvedValue(ADA_MOVED);
  });

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
      vi.mocked(api.emailChangePreview).mockRejectedValueOnce(error);
      renderAt();

      // Read, never spent, whatever the failure.
      await expectFailure(shown, UNUSED);
      expect(screen.queryByRole("button", { name: /^Yes/ })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Try again" }));

      expect(await screen.findByText(NEW)).toBeInTheDocument();
      expect(api.emailChangePreview).toHaveBeenCalledTimes(2);
      expect(api.consumeEmailChange).not.toHaveBeenCalled();
    });

    it("says so when confirming, says what it can of the link, and confirms again on a retry", async () => {
      vi.mocked(api.consumeEmailChange).mockRejectedValueOnce(error).mockResolvedValueOnce({ email: NEW });
      await confirmed("abc");

      await expectFailure(shown, afterAnswer);
      expect(updateUser).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole("button", { name: "Try again" }));

      expect(await screen.findByText(/Your sign-in address is now/)).toBeInTheDocument();
      expect(api.consumeEmailChange).toHaveBeenCalledTimes(2);
      expect(api.consumeEmailChange).toHaveBeenLastCalledWith("abc");
    });
  });

  it("calls the link dead once a retry finds the failed confirm had spent it", async () => {
    vi.mocked(api.consumeEmailChange)
      .mockRejectedValueOnce(new HttpError(500, "", null))
      .mockRejectedValueOnce(new HttpError(400, "", null));
    await confirmed();

    expect(await screen.findByText(MAY_WORK)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText(DEAD)).toBeInTheDocument();
    expect(api.consumeEmailChange).toHaveBeenCalledTimes(2);
  });

  it("offers the way out rather than a retry that would meet the same refusal", async () => {
    vi.mocked(api.consumeEmailChange).mockRejectedValue(new HttpError(422, "", null));
    await confirmed();

    expect((await screen.findByRole("alert")).textContent).toBe("The server could not accept that request.");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in instead" })).toHaveAttribute("href", "/login");
  });
});

describe("another link opened in the same page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetAuth();
    vi.mocked(api.emailChangePreview).mockImplementation(async (token: string) => ({ email: `${token}@example.com` }));
  });

  it("leaves the second link's question alone when the first link's confirm answers late", async () => {
    let answerFirst: (body: { email: string }) => void = () => {};
    vi.mocked(api.consumeEmailChange).mockReturnValue(new Promise((resolve) => (answerFirst = resolve)));
    function Other() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/auth/email/b")}>the other link</button>;
    }
    render(
      <MemoryRouter initialEntries={["/auth/email/a"]}>
        <Other />
        <Routes>
          <Route path="/auth/email/:token" element={<EmailChangePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("a@example.com");

    fireEvent.click(confirmButton());
    await act(async () => {});
    fireEvent.click(screen.getByText("the other link"));
    await screen.findByText("b@example.com");

    await act(async () => answerFirst({ email: "a@example.com" }));

    expect(screen.queryByText(/Your sign-in address is now/)).not.toBeInTheDocument();
    expect(screen.getByText("b@example.com")).toBeInTheDocument();
    expect(confirmButton()).toBeInTheDocument();
  });
});
