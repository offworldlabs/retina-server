import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import LoginPage from "../pages/LoginPage";

// Mock the auth context to return no user (unauthenticated)
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({
    user: null,
    loading: false,
    logout: vi.fn(async () => ({ redirected: false })),
    signIn: vi.fn(),
  }),
}));

const CONFIRMATION = "If that address has an account, a sign-in link is on its way.";

function answer(status: number, body: unknown = {}) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
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

  it.each([
    ["an address the server accepted", 202, { status: "accepted" }],
    ["a send that failed inside the server", 500, { detail: "smtp refused the recipient" }],
  ])("confirms without saying what happened: %s", async (_case, status, body) => {
    // The two answers must be one screen. Anything that told them apart would
    // hand back the existence check the 202 exists to withhold.
    vi.stubGlobal("fetch", answer(status, body));
    renderLogin();
    requestLink("ghost@example.com");

    expect(await screen.findByText(CONFIRMATION)).toBeInTheDocument();
    expect(screen.queryByText(/smtp refused/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
  });

  it("shows a malformed address inline and keeps the form", async () => {
    vi.stubGlobal("fetch", answer(422, { detail: [{ msg: "value is not a valid email address" }] }));
    renderLogin();
    requestLink("not-an-address");

    expect(await screen.findByText("Enter a valid email address")).toBeInTheDocument();
    expect(screen.queryByText(CONFIRMATION)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveValue("not-an-address");
  });

  it("says so when the deployment cannot send mail", async () => {
    vi.stubGlobal("fetch", answer(503, { detail: "Sign-in by email is unavailable" }));
    renderLogin();
    requestLink();

    expect(await screen.findByText("Sign-in by email is unavailable")).toBeInTheDocument();
    expect(screen.queryByText(CONFIRMATION)).not.toBeInTheDocument();
  });

  it("shows a message it is handed", () => {
    // AuthLinkPage renders this page with the reason a link failed to redeem.
    renderLogin({ message: "That sign-in link is no longer valid" });
    expect(screen.getByText("That sign-in link is no longer valid")).toBeInTheDocument();
  });
});

describe("the way back from the login card", () => {
  beforeEach(() => {
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
