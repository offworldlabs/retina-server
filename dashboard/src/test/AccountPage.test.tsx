import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HttpError } from "@retina/shared";

import { api } from "../api/client";
import Header from "../components/Header";
import RequireAuth from "../components/RequireAuth";
import Sidebar from "../components/Sidebar";
import { AuthProvider, useAuth } from "../context/AuthContext";
import { ThemeProvider } from "../context/ThemeContext";
import AccountPage from "../pages/user/AccountPage";
import { consoleRoutes } from "../routes";
import type { AccountSession } from "../types";
import { stubMatchMedia } from "./matchMedia";

/**
 * The account page: the owner's address and name, the sign-in address change,
 * the signed-in browsers and deleting the account. The real AuthProvider and
 * RequireAuth are used, with /api/auth/me answered by a stubbed fetch, since a
 * saved name has to reach the header through the shared identity and a
 * deletion has to end it the way a sign-out does. The console's own calls are
 * mocked, bar the page's own read of the account, which goes through the real
 * client so that its 401 meets what ends a session.
 */

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    api: {
      logout: vi.fn(),
      me: vi.fn(actual.api.me),
      updateMe: vi.fn(),
      deleteAccount: vi.fn(),
      mySessions: vi.fn(),
      revokeSession: vi.fn(),
      revokeOtherSessions: vi.fn(),
      requestEmailChange: vi.fn(),
    },
    adoptSession: vi.fn(() => 1),
    currentSession: vi.fn(() => 0),
    onSessionEnded: vi.fn(() => () => {}),
  };
});

const ADA = { id: "u-ada", name: "Ada", email: "ada@example.com", role: "user" };

const NOW = Date.now() / 1000;
const HOUR = 3600;
const DAY = 24 * HOUR;

/** A session row as /api/auth/me/sessions sends it. */
const session = (over: Partial<AccountSession>): AccountSession => ({
  id: "s-x",
  created_at: NOW - 3 * DAY,
  last_used_at: NOW - 30,
  expires_at: NOW + 27 * DAY,
  user_agent: "Firefox on macOS",
  ip: "203.0.113.0/24",
  current: false,
  ...over,
});

// The server's order, most recent first: this browser is not at the top.
const PHONE = session({ id: "s-phone", user_agent: "Safari on iOS", ip: "198.51.100.0/24", last_used_at: NOW - 5 });
const HERE = session({ id: "s-here", current: true, created_at: NOW - 10 * DAY, last_used_at: NOW - 60 });
const WORK = session({
  id: "s-work",
  user_agent: "Edge on Windows",
  ip: "2001:db8:1::/48",
  created_at: NOW - 40 * DAY,
  last_used_at: NOW - 2 * HOUR,
});

const httpError = (status: number, detail: unknown) => new HttpError(status, "", { detail });

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Answers /api/auth/me, the only request that reaches fetch. */
function stubServer(user: Record<string, unknown> = ADA) {
  const fetchMock = vi.fn(async (url: string) => (url === "/api/auth/me" ? json(user) : json({ detail: "Not Found" }, 404)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Where the router is, how it got there, and the page a guard's bounce
 *  would carry for after the sign-in. */
function Where() {
  const { pathname, state } = useLocation();
  const next = (state as { next?: string } | null)?.next;
  return <output aria-label="location">{`${useNavigationType()} ${pathname}${next ? ` next=${next}` : ""}`}</output>;
}

function renderAccount() {
  return render(
    <MemoryRouter initialEntries={["/overview", "/account"]} initialIndex={1}>
      <ThemeProvider>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<h1>Sign in card</h1>} />
            <Route
              path="/*"
              element={
                <RequireAuth isAdmin={false}>
                  <Header title="Account" />
                  <Routes>
                    <Route path="account" element={<AccountPage />} />
                    <Route path="overview" element={<h1>My Nodes</h1>} />
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

/** Renders the page and waits for the identity to settle. */
async function openAccount() {
  const view = renderAccount();
  await screen.findByRole("heading", { name: "Account", level: 1 });
  return view;
}

const card = (title: string) => screen.getByRole("heading", { name: title, level: 3 }).closest(".card") as HTMLElement;
const where = () => screen.getByLabelText("location");
const headerUser = () => screen.getByRole("button", { name: /^Account: / });

/** The rows of the sessions list, once it has loaded. */
async function sessionRows() {
  const list = await within(card("Sessions")).findByRole("list");
  return within(list).getAllByRole("listitem");
}

beforeEach(() => {
  stubMatchMedia();
  stubServer();
  for (const fn of Object.values(api)) vi.mocked(fn as () => unknown).mockReset();
  vi.mocked(api.mySessions).mockResolvedValue([PHONE, HERE, WORK]);
  vi.mocked(api.logout).mockResolvedValue({ ok: true });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the account page's route", () => {
  it("is on the app console alone, under the name Account", () => {
    const entry = consoleRoutes(false, { syntheticFleet: false }).find((route) => route.path === "/account");
    expect(entry && "title" in entry ? entry.title : null).toBe("Account");
    expect(consoleRoutes(true, { syntheticFleet: true }).map((route) => route.path)).not.toContain("/account");
  });

  // Reached from the account menu, which says whose account it is.
  it("is not in the sidebar", async () => {
    const { container } = render(
      <MemoryRouter>
        <AuthProvider>
          <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
        </AuthProvider>
      </MemoryRouter>,
    );
    // Signed in, so every entry is a live link, My Nodes among them.
    await waitFor(() => expect(container.querySelector('a[href="/overview"]')).not.toBeNull());
    expect(container.querySelectorAll("a").length).toBeGreaterThan(5);
    expect(container.querySelector('a[href="/account"]')).toBeNull();
    expect(screen.queryByRole("link", { name: "Account" })).toBeNull();
  });
});

describe("the account page", () => {
  it("has the four sections, under the header's own account", async () => {
    await openAccount();
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "Profile",
      "Sign-in address",
      "Sessions",
      "Delete account",
    ]);
    expect(headerUser()).toHaveAccessibleName("Account: Ada");
  });
});

describe("the account kept in step with the server", () => {
  const MOVED = { ...ADA, email: "ada@new.example.com" };
  const meCalls = (fetchMock: ReturnType<typeof stubServer>) =>
    fetchMock.mock.calls.filter(([url]) => url === "/api/auth/me").length;

  /** Answers /api/auth/me with whatever `server.me` is: the account, a
   *  failure, or the 401 of a session that has ended. */
  function liveServer(first: Record<string, unknown>) {
    const server = { me: first as Record<string, unknown> | "down" | "ended" };
    const fetchMock = vi.fn(async (url: string) => {
      if (url !== "/api/auth/me") return json({ detail: "Not Found" }, 404);
      if (server.me === "down") return json({ detail: "boom" }, 500);
      if (server.me === "ended") return json({ detail: "Not authenticated" }, 401);
      return json(server.me);
    });
    vi.stubGlobal("fetch", fetchMock);
    return { server, fetchMock };
  }

  /** The tab becoming visible or hidden, as the browser reports it. */
  function tab(state: "visible" | "hidden") {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
    document.dispatchEvent(new Event("visibilitychange"));
  }

  const realLocation = window.location;

  /** Where the client reads the page from, to decide where a 401 sends it. */
  function stubLocation(pathname: string) {
    const loc = { pathname, hostname: "app.retina.fm", search: "", href: `https://app.retina.fm${pathname}` };
    Object.defineProperty(window, "location", { value: loc, writable: true, configurable: true });
    return loc;
  }

  afterEach(() => {
    Reflect.deleteProperty(document, "visibilityState");
    Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
  });

  const shownAddress = () => within(card("Sign-in address")).getByText(/Sign-in links go to/);
  const deleteField = (address: string) => within(card("Delete account")).getByLabelText(`Type ${address} to confirm`);
  const deleteButton = () => within(card("Delete account")).getByRole("button", { name: "Delete account" });

  it("reads the account when it opens, and shows the address the server holds now", async () => {
    // The identity settled on the old address; the page's own read finds the new one.
    const answers = [ADA, MOVED];
    const fetchMock = vi.fn(async (url: string) =>
      url === "/api/auth/me" ? json(answers.shift() ?? MOVED) : json({ detail: "Not Found" }, 404),
    );
    vi.stubGlobal("fetch", fetchMock);
    await openAccount();

    await waitFor(() => expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@new.example.com."));
    expect(within(card("Profile")).getByText("ada@new.example.com")).toBeInTheDocument();
    expect(meCalls(fetchMock)).toBe(2);
    // The delete check follows: the old address is no longer the one to type.
    fireEvent.change(deleteField("ada@new.example.com"), { target: { value: "ada@example.com" } });
    expect(deleteButton()).toBeDisabled();
    fireEvent.change(deleteField("ada@new.example.com"), { target: { value: "ada@new.example.com" } });
    expect(deleteButton()).toBeEnabled();
  });

  it("reads it again when the tab becomes visible, and not while it is hidden", async () => {
    const { server, fetchMock } = liveServer(ADA);
    await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));
    expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@example.com.");

    server.me = MOVED;
    act(() => tab("hidden"));
    expect(meCalls(fetchMock)).toBe(2);
    expect(shownAddress()).toHaveTextContent("ada@example.com");

    act(() => tab("visible"));
    await waitFor(() => expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@new.example.com."));
    expect(meCalls(fetchMock)).toBe(3);
    expect(within(card("Profile")).getByText("ada@new.example.com")).toBeInTheDocument();
    expect(deleteField("ada@new.example.com")).toBeInTheDocument();
    fireEvent.change(deleteField("ada@new.example.com"), { target: { value: "ada@new.example.com" } });
    expect(deleteButton()).toBeEnabled();
  });

  it("reads it again when the window regains focus with the page already visible", async () => {
    const { server, fetchMock } = liveServer(ADA);
    await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));

    server.me = MOVED;
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@new.example.com."));
    expect(meCalls(fetchMock)).toBe(3);
  });

  it("makes one read of the focus and the visibility that arrive together", async () => {
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let calls = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url !== "/api/auth/me") return json({ detail: "Not Found" }, 404);
      calls += 1;
      if (calls > 2) await held;
      return json(MOVED);
    });
    vi.stubGlobal("fetch", fetchMock);
    await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));

    act(() => {
      tab("visible");
      window.dispatchEvent(new Event("focus"));
      tab("visible");
    });
    expect(meCalls(fetchMock)).toBe(3);

    release();
    await waitFor(() => expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@new.example.com."));
    expect(meCalls(fetchMock)).toBe(3);

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(meCalls(fetchMock)).toBe(4));
  });

  it("stops saying a link is on its way once the account holds another address", async () => {
    vi.mocked(api.requestEmailChange).mockResolvedValue({ status: "sent", email: "ada@new.example.com", expires_in: 900 });
    const { server, fetchMock } = liveServer(ADA);
    await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));
    fireEvent.change(within(card("Sign-in address")).getByLabelText("New address"), {
      target: { value: "ada@new.example.com" },
    });
    fireEvent.click(within(card("Sign-in address")).getByRole("button", { name: "Send link" }));
    const status = within(card("Sign-in address")).getByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("A link is on its way to ada@new.example.com."));

    // Confirmed in another tab; this one reads the account on coming back.
    server.me = MOVED;
    act(() => tab("visible"));
    await waitFor(() => expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@new.example.com."));
    expect(status).toBeEmptyDOMElement();
    expect(screen.queryByText(/A link is on its way/)).toBeNull();
  });

  it("changes nothing when a read fails", async () => {
    const { server, fetchMock } = liveServer(ADA);
    await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));

    server.me = "down";
    act(() => tab("visible"));
    await waitFor(() => expect(meCalls(fetchMock)).toBe(3));

    expect(shownAddress()).toHaveTextContent("Sign-in links go to ada@example.com.");
    expect(headerUser()).toHaveAccessibleName("Account: Ada");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.change(deleteField("ada@example.com"), { target: { value: "ada@example.com" } });
    expect(deleteButton()).toBeEnabled();
  });

  // The session revoked from another browser, or the account deleted by an
  // administrator, while this page sat open: the read on coming back meets a
  // 401, and the page goes to the login card with itself to return to, as
  // any owner page does.
  it("ends the session when a read finds it over, as an owner page's request does", async () => {
    const { server, fetchMock } = liveServer(ADA);
    const loc = stubLocation("/account");
    await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));
    expect(loc.href).toBe("https://app.retina.fm/account");

    server.me = "ended";
    act(() => tab("visible"));

    await waitFor(() => expect(loc.href).toBe("/login?next=%2Faccount"));
    expect(meCalls(fetchMock)).toBe(3);
  });

  it("stops reading once the page has gone", async () => {
    const { fetchMock } = liveServer(ADA);
    const view = await openAccount();
    await waitFor(() => expect(meCalls(fetchMock)).toBe(2));

    view.unmount();
    act(() => tab("visible"));
    expect(meCalls(fetchMock)).toBe(2);
  });
});

describe("the profile", () => {
  it("shows the address as text, not as a field", async () => {
    await openAccount();
    const address = within(card("Profile")).getByText("ada@example.com");
    expect(address.closest("input, button, [role='textbox']")).toBeNull();
    expect(within(card("Profile")).queryByRole("textbox")).toBeNull();
  });

  it("saves a new name, and the header shows it at once, with no reload", async () => {
    const fetchMock = stubServer();
    vi.mocked(api.updateMe).mockResolvedValue({ ...ADA, name: "Ada Lovelace" });
    await openAccount();

    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    const field = screen.getByRole("textbox", { name: "Display name" });
    expect(field).toHaveValue("Ada");
    expect(field).toHaveFocus();

    fireEvent.change(field, { target: { value: "Ada Lovelace" } });
    fireEvent.click(within(card("Profile")).getByRole("button", { name: "Save" }));

    expect(api.updateMe).toHaveBeenCalledWith({ name: "Ada Lovelace" });
    expect(await screen.findByRole("button", { name: "Account: Ada Lovelace" })).toBeInTheDocument();
    expect(within(card("Profile")).getByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(card("Profile")).getByRole("status")).toHaveTextContent("Name saved.");
    expect(screen.getByRole("button", { name: "Edit display name" })).toHaveFocus();
    // Asked at the start, by the identity and by the page: the header took the
    // save's own answer.
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/auth/me")).toHaveLength(2);
    // Still the same session's page, not a fresh one.
    expect(where()).toHaveTextContent("/account");
  });

  it("keeps fields /api/auth/me reported that the save's answer leaves out", async () => {
    stubServer({ ...ADA, polled_radar_registration: true });
    vi.mocked(api.updateMe).mockResolvedValue({ id: ADA.id, email: ADA.email, name: "Ada L" });
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Ada L" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Display name" }));
    expect(await screen.findByRole("button", { name: "Account: Ada L" })).toBeInTheDocument();
    // The page is still drawn from a signed-in identity carrying the address.
    expect(within(card("Profile")).getByText("ada@example.com")).toBeInTheDocument();
  });

  it.each([
    ["Cancel", () => fireEvent.click(within(card("Profile")).getByRole("button", { name: "Cancel" }))],
    ["Escape", () => fireEvent.keyDown(screen.getByRole("textbox", { name: "Display name" }), { key: "Escape" })],
  ])("puts the name back on %s, sending nothing", async (_how, cancel) => {
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Someone" } });
    cancel();
    expect(screen.queryByRole("textbox", { name: "Display name" })).toBeNull();
    expect(within(card("Profile")).getByText("Ada")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit display name" })).toHaveFocus();
    expect(api.updateMe).not.toHaveBeenCalled();
  });

  it("closes an unchanged name without asking the server", async () => {
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "  Ada " } });
    fireEvent.click(within(card("Profile")).getByRole("button", { name: "Save" }));
    expect(screen.queryByRole("textbox", { name: "Display name" })).toBeNull();
    expect(api.updateMe).not.toHaveBeenCalled();
  });

  it("shows the server's 422 at the field, keeping the draft and the header as they were", async () => {
    vi.mocked(api.updateMe).mockRejectedValue(httpError(422, "A name is at most 64 characters; this one has 70."));
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    const field = screen.getByRole("textbox", { name: "Display name" });
    fireEvent.change(field, { target: { value: "x".repeat(70) } });
    fireEvent.click(within(card("Profile")).getByRole("button", { name: "Save" }));

    const alert = await within(card("Profile")).findByRole("alert");
    expect(alert).toHaveTextContent("A name is at most 64 characters; this one has 70.");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(/A name is at most 64 characters/);
    expect(field).toHaveValue("x".repeat(70));
    expect(field).toHaveFocus();
    expect(headerUser()).toHaveAccessibleName("Account: Ada");

    // Typing again takes the refusal away.
    fireEvent.change(field, { target: { value: "Ada L" } });
    expect(within(card("Profile")).queryByRole("alert")).toBeNull();
    expect(field).not.toHaveAttribute("aria-invalid");
  });

  it("states the rules for a 422 whose detail is not a sentence", async () => {
    vi.mocked(api.updateMe).mockRejectedValue(httpError(422, [{ msg: "field required" }]));
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "\u0007" } });
    fireEvent.click(within(card("Profile")).getByRole("button", { name: "Save" }));
    expect(await within(card("Profile")).findByRole("alert")).toHaveTextContent(
      "A name needs 1 to 64 characters and no control characters.",
    );
  });

  it("says any other failure in the shared words, with a retry that saves", async () => {
    vi.mocked(api.updateMe)
      .mockRejectedValueOnce({ status: 503 })
      .mockResolvedValueOnce({ ...ADA, name: "Ada L" });
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    const field = screen.getByRole("textbox", { name: "Display name" });
    fireEvent.change(field, { target: { value: "Ada L" } });
    fireEvent.click(within(card("Profile")).getByRole("button", { name: "Save" }));

    const alert = await within(card("Profile")).findByRole("alert");
    expect(alert).toHaveTextContent("Could not save your name. This service is unavailable right now. Try again later.");
    // Not the field's fault.
    expect(field).not.toHaveAttribute("aria-invalid");

    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Account: Ada L" })).toBeInTheDocument();
    expect(api.updateMe).toHaveBeenCalledTimes(2);
  });

  it("says when there is no name, and that the header shows the address instead", async () => {
    stubServer({ ...ADA, name: "" });
    await openAccount();
    expect(within(card("Profile")).getByText("Not set")).toBeInTheDocument();
    expect(within(card("Profile")).getByText("Without one, the header shows your address.")).toBeInTheDocument();
  });
});

describe("the shared identity a saved name goes to", () => {
  it("leaves the account signed out when the save answers after a sign-out", async () => {
    let answer: (user: typeof ADA) => void = () => {};
    vi.mocked(api.updateMe).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    await openAccount();
    fireEvent.click(screen.getByRole("button", { name: "Edit display name" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Ada L" } });
    fireEvent.click(within(card("Profile")).getByRole("button", { name: "Save" }));

    fireEvent.click(headerUser());
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();

    await act(async () => answer({ ...ADA, name: "Ada L" }));
    expect(screen.queryByRole("button", { name: /^Account: / })).toBeNull();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("takes a newer copy of the account signed in, and of no other", async () => {
    function Copies() {
      const { updateUser } = useAuth();
      return (
        <>
          <button type="button" onClick={() => updateUser({ id: "u-bob", name: "Bob", email: "bob@example.com" })}>
            another account
          </button>
          <button type="button" onClick={() => updateUser({ id: ADA.id, name: "Ada L", email: ADA.email })}>
            this account
          </button>
        </>
      );
    }
    render(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <Header title="Account" />
            <Copies />
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    );
    await screen.findByRole("button", { name: "Account: Ada" });
    fireEvent.click(screen.getByRole("button", { name: "another account" }));
    expect(headerUser()).toHaveAccessibleName("Account: Ada");
    fireEvent.click(screen.getByRole("button", { name: "this account" }));
    expect(headerUser()).toHaveAccessibleName("Account: Ada L");
  });
});

describe("changing the sign-in address", () => {
  const field = () => within(card("Sign-in address")).getByLabelText("New address");
  const send = () => within(card("Sign-in address")).getByRole("button", { name: "Send link" });

  it("mails the new address, and says where the link went, how long it lasts and what still works", async () => {
    vi.mocked(api.requestEmailChange).mockResolvedValue({ status: "sent", email: "new@example.com", expires_in: 900 });
    await openAccount();
    expect(field()).toHaveAttribute("type", "email");
    fireEvent.change(field(), { target: { value: "  New@Example.com " } });
    fireEvent.click(send());

    expect(api.requestEmailChange).toHaveBeenCalledWith("New@Example.com");
    const status = within(card("Sign-in address")).getByRole("status");
    await waitFor(() =>
      expect(status).toHaveTextContent(
        "A link is on its way to new@example.com. It lasts 15 minutes. Until it is opened, ada@example.com still signs you in.",
      ),
    );
    expect(field()).toHaveValue("");
    // The address in use has not changed yet.
    expect(within(card("Profile")).getByText("ada@example.com")).toBeInTheDocument();
  });

  it("offers Send only once something is typed", async () => {
    await openAccount();
    expect(send()).toBeDisabled();
    fireEvent.change(field(), { target: { value: "   " } });
    expect(send()).toBeDisabled();
    fireEvent.change(field(), { target: { value: "new@example.com" } });
    expect(send()).toBeEnabled();
  });

  it.each([
    [409, "That address belongs to another account"],
    [400, "That is already this account's address"],
    [400, "That is not an email address"],
  ])("shows the server's own reason for a %i at the field", async (status, detail) => {
    vi.mocked(api.requestEmailChange).mockRejectedValue(httpError(status, detail));
    await openAccount();
    fireEvent.change(field(), { target: { value: "taken@example.com" } });
    fireEvent.click(send());
    expect(await within(card("Sign-in address")).findByRole("alert")).toHaveTextContent(detail);
    expect(field()).toHaveAttribute("aria-invalid", "true");
    expect(field()).toHaveAccessibleDescription(detail);
    expect(within(card("Sign-in address")).getByRole("status")).toBeEmptyDOMElement();
  });

  it.each([
    ["a rate limit", { status: 429 }, "Too many links have been asked for. Try again later."],
    ["mail that cannot leave", { status: 503 }, "This service is unavailable right now. Try again later."],
    [
      "a connection that never answered",
      new TypeError("Failed to fetch"),
      "Could not reach the server. Check your connection, then try again.",
    ],
  ])("says %s in the shared words, without blaming the address", async (_what, failure, message) => {
    vi.mocked(api.requestEmailChange).mockRejectedValue(failure);
    await openAccount();
    fireEvent.change(field(), { target: { value: "new@example.com" } });
    fireEvent.click(send());
    expect(await within(card("Sign-in address")).findByRole("alert")).toHaveTextContent(message);
    expect(field()).not.toHaveAttribute("aria-invalid");
    // The address stays, to send again.
    expect(field()).toHaveValue("new@example.com");
  });
});

describe("the sessions", () => {
  it("shows a loading state until the list arrives", async () => {
    vi.mocked(api.mySessions).mockReturnValue(new Promise(() => {}));
    await openAccount();
    expect(within(card("Sessions")).getByText("Loading…")).toBeInTheDocument();
  });

  it("lists each browser with its device, network, last activity and sign-in day, this one first", async () => {
    await openAccount();
    const rows = await sessionRows();
    expect(rows.map((row) => within(row).getByText(/ on /).textContent)).toEqual([
      "Firefox on macOS",
      "Safari on iOS",
      "Edge on Windows",
    ]);
    const [here, phone, work] = rows;
    expect(within(here).getByText("This browser")).toBeInTheDocument();
    expect(within(phone).queryByText("This browser")).toBeNull();
    expect(within(work).queryByText("This browser")).toBeNull();

    expect(within(phone).getByText("198.51.100.0/24")).toBeInTheDocument();
    expect(within(work).getByText("2001:db8:1::/48")).toBeInTheDocument();
    expect(within(work).getByText("2h ago")).toBeInTheDocument();
    const day = new Date(WORK.created_at * 1000).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
    expect(within(work).getByText(day)).toHaveAttribute("dateTime", new Date(WORK.created_at * 1000).toISOString());
    for (const label of ["Network", "Last active", "Signed in"]) expect(within(work).getByText(label)).toBeInTheDocument();
  });

  it("offers no sign-out for this browser", async () => {
    await openAccount();
    const [here, phone, work] = await sessionRows();
    expect(within(here).queryByRole("button")).toBeNull();
    expect(within(phone).getByRole("button", { name: "Sign out Safari on iOS" })).toBeInTheDocument();
    expect(within(work).getByRole("button", { name: "Sign out Edge on Windows" })).toBeInTheDocument();
  });

  it("describes each Sign out by the row it ends", async () => {
    await openAccount();
    await sessionRows();
    expect(screen.getByRole("button", { name: "Sign out Safari on iOS" })).toHaveAccessibleDescription(
      /Network\s*198\.51\.100\.0\/24/,
    );
  });

  it("names a browser the server could not tell as unknown", async () => {
    vi.mocked(api.mySessions).mockResolvedValue([HERE, session({ id: "s-null", user_agent: null, ip: null })]);
    await openAccount();
    const [, unknown] = await sessionRows();
    expect(within(unknown).getByText("Unknown browser")).toBeInTheDocument();
    expect(within(unknown).getByRole("button", { name: "Sign out Unknown browser" })).toBeInTheDocument();
  });

  it("signs one other browser out and takes its row away", async () => {
    vi.mocked(api.revokeSession).mockResolvedValue({ ok: true });
    await openAccount();
    await sessionRows();
    fireEvent.click(screen.getByRole("button", { name: "Sign out Safari on iOS" }));
    expect(api.revokeSession).toHaveBeenCalledWith("s-phone");

    const status = within(card("Sessions")).getByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("Signed out Safari on iOS."));
    expect(status).toHaveFocus();
    const rows = await sessionRows();
    expect(rows).toHaveLength(2);
    expect(screen.queryByText("Safari on iOS")).toBeNull();
  });

  it("holds the other buttons while one sign-out is in flight", async () => {
    let answer: (value: { ok: true }) => void = () => {};
    vi.mocked(api.revokeSession).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    await openAccount();
    await sessionRows();
    fireEvent.click(screen.getByRole("button", { name: "Sign out Safari on iOS" }));
    expect(screen.getByRole("button", { name: "Sign out Safari on iOS" })).toHaveTextContent("Signing out…");
    expect(screen.getByRole("button", { name: "Sign out Edge on Windows" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign out everywhere else" })).toBeDisabled();
    await act(async () => answer({ ok: true }));
    expect(screen.getByRole("button", { name: "Sign out Edge on Windows" })).toBeEnabled();
  });

  it("takes a session that had already ended as signed out", async () => {
    vi.mocked(api.revokeSession).mockRejectedValue(httpError(404, "Session not found"));
    await openAccount();
    await sessionRows();
    fireEvent.click(screen.getByRole("button", { name: "Sign out Safari on iOS" }));
    await waitFor(() =>
      expect(within(card("Sessions")).getByRole("status")).toHaveTextContent("Safari on iOS was already signed out."),
    );
    expect(await sessionRows()).toHaveLength(2);
  });

  it("keeps the row when the sign-out fails, and says why with a retry", async () => {
    vi.mocked(api.revokeSession).mockRejectedValueOnce({ status: 503 }).mockResolvedValueOnce({ ok: true });
    await openAccount();
    await sessionRows();
    fireEvent.click(screen.getByRole("button", { name: "Sign out Safari on iOS" }));
    const alert = await within(card("Sessions")).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not sign out Safari on iOS. This service is unavailable right now. Try again later.",
    );
    expect(await sessionRows()).toHaveLength(3);

    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText("Safari on iOS")).toBeNull());
    expect(api.revokeSession).toHaveBeenCalledTimes(2);
  });

  it("signs out everywhere else, leaving this browser", async () => {
    vi.mocked(api.revokeOtherSessions).mockResolvedValue({ revoked: 2 });
    await openAccount();
    await sessionRows();
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere else" }));
    expect(api.revokeOtherSessions).toHaveBeenCalledTimes(1);

    const status = within(card("Sessions")).getByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("Signed out 2 other sessions."));
    expect(status).toHaveFocus();
    const rows = await sessionRows();
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("This browser")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out everywhere else" })).toBeNull();
    expect(within(card("Sessions")).getByText("No other browser is signed in to your account.")).toBeInTheDocument();
  });

  it("says a failed sign-out everywhere else in the shared words, with a retry", async () => {
    vi.mocked(api.revokeOtherSessions).mockRejectedValue({ status: 429 });
    await openAccount();
    await sessionRows();
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere else" }));
    const alert = await within(card("Sessions")).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not sign out the other sessions. Too many attempts from this network. Wait a minute, then try again.",
    );
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(await sessionRows()).toHaveLength(3);
  });

  it("offers Sign out everywhere else only while there are others", async () => {
    vi.mocked(api.mySessions).mockResolvedValue([HERE]);
    await openAccount();
    const rows = await sessionRows();
    expect(rows).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Sign out everywhere else" })).toBeNull();
    expect(within(card("Sessions")).getByText("No other browser is signed in to your account.")).toBeInTheDocument();
  });

  it("says when there is nothing to list", async () => {
    vi.mocked(api.mySessions).mockResolvedValue([]);
    await openAccount();
    expect(await within(card("Sessions")).findByText("No signed-in browsers to show.")).toBeInTheDocument();
    expect(within(card("Sessions")).queryByRole("list")).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign out everywhere else" })).toBeNull();
  });

  it("says a failed load with a retry that asks again", async () => {
    vi.mocked(api.mySessions)
      .mockRejectedValueOnce(new HttpError(500, "Internal Server Error", null))
      .mockResolvedValueOnce([PHONE, HERE]);
    vi.spyOn(console, "error").mockImplementation(() => {});
    await openAccount();
    const alert = await within(card("Sessions")).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not load your sessions. The server ran into a problem (HTTP 500). Try again in a moment.",
    );
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await sessionRows()).toHaveLength(2);
    expect(api.mySessions).toHaveBeenCalledTimes(2);
    expect(within(card("Sessions")).queryByRole("alert")).toBeNull();
  });
});

describe("deleting the account", () => {
  const field = () => within(card("Delete account")).getByLabelText("Type ada@example.com to confirm");
  const start = () => within(card("Delete account")).getByRole("button", { name: "Delete account" });
  const confirm = () => screen.getByRole("button", { name: "Yes, delete my account" });

  async function askToDelete(typed = "ada@example.com") {
    await openAccount();
    fireEvent.change(field(), { target: { value: typed } });
    fireEvent.click(start());
  }

  it("says what it does, and that it cannot be undone", async () => {
    await openAccount();
    const text = card("Delete account").textContent ?? "";
    expect(text).toContain("releases your nodes");
    expect(text).toContain("signs out every browser");
    expect(text).toContain("removes the account");
    expect(text).toContain("It cannot be undone.");
  });

  it("keeps Delete off until the typed address matches, trimmed and in any case", async () => {
    await openAccount();
    expect(start()).toBeDisabled();
    for (const near of ["ada", "ada@example.co", "ada@example.com.", "bob@example.com"]) {
      fireEvent.change(field(), { target: { value: near } });
      expect(start()).toBeDisabled();
    }
    fireEvent.change(field(), { target: { value: "  ADA@Example.COM " } });
    expect(start()).toBeEnabled();
    fireEvent.change(field(), { target: { value: "" } });
    expect(start()).toBeDisabled();
  });

  it("does not take Enter in the field as a delete before the address matches", async () => {
    await openAccount();
    fireEvent.change(field(), { target: { value: "ada@" } });
    fireEvent.submit(field());
    expect(screen.queryByRole("group", { name: /Delete your account\?/ })).toBeNull();
  });

  it("asks once more before deleting, and Keep my account goes back", async () => {
    await askToDelete();
    const question = screen.getByRole("group", { name: "Delete your account? This cannot be undone." });
    expect(question).toBeInTheDocument();
    expect(within(question).getByRole("button", { name: "Keep my account" })).toHaveFocus();
    expect(field()).toBeDisabled();
    expect(api.deleteAccount).not.toHaveBeenCalled();

    fireEvent.click(within(question).getByRole("button", { name: "Keep my account" }));
    expect(screen.queryByRole("group", { name: /Delete your account\?/ })).toBeNull();
    expect(start()).toHaveFocus();
    expect(field()).toBeEnabled();
    expect(api.deleteAccount).not.toHaveBeenCalled();
  });

  it("deletes, drops the identity as a sign-out does, and the login card says the account is gone", async () => {
    vi.mocked(api.deleteAccount).mockResolvedValue({ ok: true, released: 2 });
    await askToDelete("  ADA@example.com ");
    fireEvent.click(confirm());
    expect(api.deleteAccount).toHaveBeenCalledWith("ADA@example.com");

    expect(await screen.findByRole("heading", { name: "Sign in card" })).toBeInTheDocument();
    // The page's own way out, replacing it in history, not the guard's bounce,
    // which would carry the account page to return to after a sign-in.
    expect(where()).toHaveTextContent(/^REPLACE \/login$/);
    expect(screen.queryByRole("button", { name: /^Account: / })).toBeNull();

    const notice = screen.getByText("Account deleted").closest(".signout-notice");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent("Account deleted");
    expect(notice).toHaveTextContent("Your 2 nodes were released and every session has ended.");
    // Nothing else to end: the deletion's answer cleared the cookie.
    expect(api.logout).not.toHaveBeenCalled();
  });

  it("puts focus on the news, since the button that was pressed has gone", async () => {
    vi.mocked(api.deleteAccount).mockResolvedValue({ ok: true, released: 0 });
    await askToDelete();
    fireEvent.click(confirm());
    await screen.findByRole("heading", { name: "Sign in card" });
    expect(screen.getByText("Every session has ended.")).toHaveFocus();
  });

  it.each([
    [0, "Every session has ended."],
    [1, "Your node was released and every session has ended."],
  ])("says in words how many nodes were released: %i", async (released, words) => {
    vi.mocked(api.deleteAccount).mockResolvedValue({ ok: true, released });
    await askToDelete();
    fireEvent.click(confirm());
    expect(await screen.findByText(words)).toBeInTheDocument();
  });

  it("lets the notice be put away", async () => {
    vi.mocked(api.deleteAccount).mockResolvedValue({ ok: true, released: 1 });
    await askToDelete();
    fireEvent.click(confirm());
    await screen.findByRole("heading", { name: "Sign in card" });
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Account deleted")).toBeNull();
  });

  it("sends a refused address back to the field with the server's reason, still signed in", async () => {
    vi.mocked(api.deleteAccount).mockRejectedValue(httpError(400, "That is not this account's address"));
    await askToDelete();
    fireEvent.click(confirm());

    const alert = await within(card("Delete account")).findByRole("alert");
    expect(alert).toHaveTextContent("That is not this account's address");
    expect(field()).toHaveAttribute("aria-invalid", "true");
    expect(field()).toHaveAccessibleDescription("That is not this account's address");
    expect(field()).toHaveFocus();
    expect(screen.queryByRole("group", { name: /Delete your account\?/ })).toBeNull();
    expect(where()).toHaveTextContent("/account");
    expect(headerUser()).toHaveAccessibleName("Account: Ada");
  });

  it("says any other failure at the question in the shared words, with a retry that deletes", async () => {
    vi.mocked(api.deleteAccount)
      .mockRejectedValueOnce({ status: 503 })
      .mockResolvedValueOnce({ ok: true, released: 0 });
    await askToDelete();
    fireEvent.click(confirm());

    const alert = await within(card("Delete account")).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not delete your account. This service is unavailable right now. Try again later.",
    );
    expect(headerUser()).toHaveAccessibleName("Account: Ada");
    expect(screen.getByRole("group", { name: /Delete your account\?/ })).toBeInTheDocument();

    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Sign in card" })).toBeInTheDocument();
    expect(api.deleteAccount).toHaveBeenCalledTimes(2);
  });

  it("holds both answers while the deletion is in flight", async () => {
    vi.mocked(api.deleteAccount).mockReturnValue(new Promise(() => {}));
    await askToDelete();
    fireEvent.click(confirm());
    expect(screen.getByRole("button", { name: "Deleting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keep my account" })).toBeDisabled();
  });
});

describe("the account calls in the API client", () => {
  type Client = (typeof import("../api/client"))["api"];

  const calls: [string, string, string, (client: Client) => Promise<unknown>, unknown][] = [
    ["me", "GET", "/api/auth/me", (c) => c.me(), undefined],
    ["updateMe", "PATCH", "/api/auth/me", (c) => c.updateMe({ name: "Ada" }), { name: "Ada" }],
    ["deleteAccount", "POST", "/api/auth/me/delete", (c) => c.deleteAccount("ada@example.com"), { email: "ada@example.com" }],
    ["mySessions", "GET", "/api/auth/me/sessions", (c) => c.mySessions(), undefined],
    ["revokeSession", "DELETE", "/api/auth/me/sessions/s%2F1%202", (c) => c.revokeSession("s/1 2"), undefined],
    ["revokeOtherSessions", "POST", "/api/auth/me/sessions/revoke-others", (c) => c.revokeOtherSessions(), undefined],
    ["requestEmailChange", "POST", "/api/auth/me/email", (c) => c.requestEmailChange("new@example.com"), { email: "new@example.com" }],
    ["emailChangePreview", "GET", "/api/auth/email-change/t%2Fk%20n", (c) => c.emailChangePreview("t/k n"), undefined],
    ["consumeEmailChange", "POST", "/api/auth/email-change/consume", (c) => c.consumeEmailChange("t/k"), { token: "t/k" }],
    ["resendClaimByToken", "POST", "/api/auth/claim/resend", (c) => c.resendClaimByToken("t/k"), { token: "t/k" }],
    ["myClaims", "GET", "/api/auth/me/claims", (c) => c.myClaims(), undefined],
    ["resendMyClaim", "POST", "/api/auth/me/claims/nde%201%2Fx/resend", (c) => c.resendMyClaim("nde 1/x"), undefined],
    [
      "adminChangeUserEmail",
      "POST",
      "/api/admin/users/u%2F1/email",
      (c) => c.adminChangeUserEmail("u/1", "new@example.com"),
      { email: "new@example.com" },
    ],
  ];

  it.each(calls)("%s sends %s to %s", async (_name, method, path, call, body) => {
    const { api: client } = await vi.importActual<typeof import("../api/client")>("../api/client");
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(call(client)).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(path);
    expect(init?.method ?? "GET").toBe(method);
    expect(init?.body === undefined ? undefined : JSON.parse(String(init.body))).toEqual(body);
  });
});
