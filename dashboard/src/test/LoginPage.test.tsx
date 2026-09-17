import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
    // The OAuth providers are gone from the backend's usable paths; an anchor
    // left here would take a caller to a route that cannot sign them in.
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

  it("shows a message it is handed instead of the OAuth one", () => {
    renderLogin({ message: "That sign-in link is no longer valid" });
    expect(screen.getByText("That sign-in link is no longer valid")).toBeInTheDocument();
  });
});
