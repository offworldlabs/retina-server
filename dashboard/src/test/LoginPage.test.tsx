import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";
import LoginPage from "../pages/LoginPage";
import { resetAuth, setAuth } from "./fakeAuth";

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

const CONFIRMATION = /^A sign-in link is on its way to /;

function answer(status: number, body: unknown = {}) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      })
  );
}

/** A request that never answers, as one to a server that has gone quiet: it
 *  ends only when the shared request's own timer aborts it, with the reason
 *  that timer gave, as fetch does. */
function silence() {
  return vi.fn(
    (_path: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal.reason));
      })
  );
}

function renderLogin(props = {}) {
  return render(
    <MemoryRouter>
      <LoginPage {...props} />
    </MemoryRouter>
  );
}

function requestLink(address = "someone@example.com") {
  fireEvent.change(screen.getByLabelText("Email address"), { target: { value: address } });
  fireEvent.click(screen.getByRole("button", { name: /sign-in link/i }));
}

describe("LoginPage", () => {
  beforeEach(() => {
    resetAuth();
    vi.stubGlobal("fetch", answer(202, { status: "accepted" }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the login card", () => {
    renderLogin();
    expect(screen.getByText("Retina")).toBeInTheDocument();
    expect(screen.getByText("Passive Radar Network Dashboard")).toBeInTheDocument();
  });

  it("offers no third-party sign-in", () => {
    // A mailed link is the only way in. An anchor here would take a caller to a
    // route that cannot sign them in.
    renderLogin();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.queryByText(/Continue with/i)).not.toBeInTheDocument();
  });

  it("posts the address to the magic-link endpoint", async () => {
    renderLogin();
    requestLink("pilot@example.com");

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const [path, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(path).toBe("/api/auth/magic-link");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ email: "pilot@example.com" });
  });

  it("confirms an address the server accepted", async () => {
    // 202 whatever the address is, quota included, so this one screen is what
    // every address that reaches the server gets.
    renderLogin();
    requestLink("ghost@example.com");

    expect(await screen.findByText("A sign-in link is on its way to ghost@example.com.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
  });

  it("shows a message it is handed", () => {
    // AuthLinkPage renders this page with the reason a link failed to redeem.
    renderLogin({ message: "That sign-in link is no longer valid" });
    expect(screen.getByText("That sign-in link is no longer valid")).toBeInTheDocument();
  });
});

describe("a request for a link that fails", () => {
  beforeEach(() => {
    resetAuth();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  /** The card once the request has failed: the error it announced, and the
   *  form, still holding the address, ready to send again. */
  function expectFailureShown(shown: string) {
    expect(screen.getByRole("alert").textContent).toBe(shown);
    expect(screen.queryByText(CONFIRMATION)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveValue("pilot@example.com");
    expect(screen.getByRole("button", { name: "Email me a sign-in link" })).toBeEnabled();
  }

  it.each([
    ["a rate limit", 429, "Too many attempts from this network. Wait a minute, then try again."],
    ["a malformed address", 422, "Enter a valid email address"],
    ["a deployment with no mail", 503, "Sign-in by email is unavailable"],
    ["a server error", 500, "The server ran into a problem (HTTP 500). Try again in a moment."],
    ["a proxy with no app behind it", 502, "Could not reach the server. Check your connection, then try again."],
  ])("says so when it meets %s (%i)", async (_case, status, shown) => {
    vi.stubGlobal("fetch", answer(status, { detail: "smtp refused the recipient" }));
    renderLogin();
    requestLink("pilot@example.com");

    await screen.findByRole("alert");
    expectFailureShown(shown);
    // The server's own words are for its log, not for the card.
    expect(screen.queryByText(/smtp refused/i)).not.toBeInTheDocument();
  });

  it("says so when the connection drops", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      })
    );
    renderLogin();
    requestLink("pilot@example.com");

    await screen.findByRole("alert");
    expectFailureShown("Could not reach the server. Check your connection, then try again.");
  });

  it("says so when the server does not answer in time", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", silence());
    renderLogin();
    requestLink("pilot@example.com");
    expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expectFailureShown("The server did not answer in time. Try again.");
  });

  it("marks the address itself as the problem only when the server said it was", async () => {
    vi.stubGlobal("fetch", answer(422, { detail: [{ msg: "value is not a valid email address" }] }));
    renderLogin();
    requestLink("pilot@example.com");
    await screen.findByRole("alert");

    const field = screen.getByLabelText("Email address");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription("Enter a valid email address");
  });

  it("does not blame the address for a failure that is not about it", async () => {
    vi.stubGlobal("fetch", answer(429));
    renderLogin();
    requestLink("pilot@example.com");
    await screen.findByRole("alert");

    const field = screen.getByLabelText("Email address");
    expect(field).not.toHaveAttribute("aria-invalid");
    expect(field).toHaveAccessibleDescription("Too many attempts from this network. Wait a minute, then try again.");
  });

  it("sends again once the server recovers, and drops the error", async () => {
    vi.stubGlobal("fetch", answer(503));
    renderLogin();
    requestLink("pilot@example.com");
    await screen.findByRole("alert");

    vi.stubGlobal("fetch", answer(202, { status: "accepted" }));
    fireEvent.click(screen.getByRole("button", { name: "Email me a sign-in link" }));

    expect(await screen.findByText("A sign-in link is on its way to pilot@example.com.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("a caller who is already signed in", () => {
  const ADA = { id: "u-1", email: "ada@example.com", name: "Ada" };

  function Where() {
    const { pathname } = useLocation();
    return <output aria-label="location">{`${useNavigationType()} ${pathname}`}</output>;
  }

  function open(entry: string | { pathname: string; state: unknown }, { isAdmin = false } = {}) {
    return render(
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/login" element={<LoginPage isAdmin={isAdmin} />} />
          <Route path="/" element={<div>Dashboard</div>} />
          <Route path="/onboarding" element={<div>My Nodes page</div>} />
        </Routes>
        <Where />
      </MemoryRouter>
    );
  }

  /** What React and the router say about a navigation made while rendering:
   *  React, that a component updated another mid-render; the router, that
   *  navigate() belongs in an effect (and it drops such a call on the floor). */
  const RENDER_PHASE_NAVIGATION = /Cannot update a component|navigate\(\) in a React\.useEffect/;

  let logged: ReturnType<typeof vi.spyOn>[];

  function renderPhaseWarnings() {
    return logged.flatMap((spy) => spy.mock.calls.map((args) => args.map(String).join(" "))).filter((line) =>
      RENDER_PHASE_NAVIGATION.test(line)
    );
  }

  beforeEach(() => {
    resetAuth();
    vi.stubGlobal("fetch", answer(202, { status: "accepted" }));
    logged = [vi.spyOn(console, "error"), vi.spyOn(console, "warn")];
  });

  afterEach(() => {
    for (const spy of logged) spy.mockRestore();
    vi.unstubAllGlobals();
  });

  it.each([
    ["the page it was sent here from", { pathname: "/login", state: { next: "/onboarding" } }, "My Nodes page", "/onboarding"],
    ["the page in its URL", "/login?next=/onboarding", "My Nodes page", "/onboarding"],
    ["the console's root when nothing was asked for", "/login", "Dashboard", "/"],
  ])("is sent on to %s, without the card", (_case, entry, page, path) => {
    setAuth({ user: ADA });
    open(entry);

    expect(screen.getByText(page)).toBeInTheDocument();
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
    // In place of the card, so Back does not return to it.
    expect(screen.getByLabelText("location")).toHaveTextContent(`REPLACE ${path}`);
    expect(renderPhaseWarnings()).toEqual([]);
  });

  it("is sent to the root on the admin console, whatever the entry asked for", () => {
    setAuth({ user: ADA });
    open({ pathname: "/login", state: { next: "/onboarding" } }, { isAdmin: true });

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(renderPhaseWarnings()).toEqual([]);
  });

  it("is sent on once the session settles after the card was drawn", () => {
    setAuth({ loading: true });
    open({ pathname: "/login", state: { next: "/onboarding" } });
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();

    act(() => setAuth({ loading: false, user: ADA }));

    expect(screen.getByText("My Nodes page")).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("REPLACE /onboarding");
    expect(renderPhaseWarnings()).toEqual([]);
  });
});

describe("the page a sign-in ends on", () => {
  beforeEach(() => {
    resetAuth();
    vi.stubGlobal("fetch", answer(202, { status: "accepted" }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function requestedBody(entry, { isAdmin = false } = {}) {
    render(
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="*" element={<LoginPage isAdmin={isAdmin} />} />
        </Routes>
      </MemoryRouter>
    );
    requestLink("pilot@example.com");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    return JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body);
  }

  it("asks for a link that opens the page the visitor was sent here from", async () => {
    const body = await requestedBody({ pathname: "/login", state: { next: "/onboarding" } });
    expect(body).toEqual({ email: "pilot@example.com", next: "/onboarding" });
  });

  // A dead mailed link renders this card at its own URL, which carries the page.
  it("keeps the page a dead link was carrying", async () => {
    const body = await requestedBody("/auth/link/dead?next=/detections");
    expect(body).toEqual({ email: "pilot@example.com", next: "/detections" });
  });

  it("asks for nothing it would not open", async () => {
    const body = await requestedBody({ pathname: "/login", state: { next: "//evil.example.com" } });
    expect(body).toEqual({ email: "pilot@example.com" });
  });

  // The mailed link opens on the app host, where no admin route exists.
  it.each([
    ["handed over", { pathname: "/login", state: { next: "/nodes" } }],
    ["in the URL", "/login?next=/nodes"],
  ])("asks for no page from the admin console (%s)", async (_case, entry) => {
    const body = await requestedBody(entry, { isAdmin: true });
    expect(body).toEqual({ email: "pilot@example.com" });
  });
});

describe("the way back from the login card", () => {
  beforeEach(() => {
    resetAuth();
    vi.stubGlobal("fetch", answer(202, { status: "accepted" }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function openDirectly(props = {}) {
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <Routes>
          <Route path="/login" element={<LoginPage {...props} />} />
          <Route path="/map" element={<div>Map page</div>} />
        </Routes>
      </MemoryRouter>
    );
  }

  it("takes a visitor who arrived directly to the map", () => {
    openDirectly();
    fireEvent.click(screen.getByRole("button", { name: "Back to the map" }));
    expect(screen.getByText("Map page")).toBeInTheDocument();
  });

  it("is still there once the link is on its way", async () => {
    openDirectly();
    requestLink();
    await screen.findByText(CONFIRMATION);
    expect(screen.getByRole("button", { name: "Back to the map" })).toBeInTheDocument();
  });

  it.each([
    ["arriving directly", "/login"],
    ["marked as from an open page", { pathname: "/login", state: { fromOpenPage: true } }],
  ])("is not offered on the admin console, %s", (_case, entry) => {
    // No admin route opens without a session, so any destination would put
    // the caller straight back on this card.
    render(
      <MemoryRouter initialEntries={[entry]}>
        <LoginPage isAdmin />
      </MemoryRouter>
    );
    expect(screen.queryByRole("button", { name: /back/i })).not.toBeInTheDocument();
  });
});
