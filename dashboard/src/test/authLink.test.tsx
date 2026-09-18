import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import AuthLinkPage from "../pages/AuthLinkPage";

/**
 * The page the mailed link opens.
 *
 * Redemption is one-shot, so the count of POSTs is part of the behaviour: a
 * second one finds the token already spent and reports a dead link for a
 * sign-in that worked. StrictMode's second effect pass is where that happens,
 * so every case below renders under it.
 */

const signIn = vi.hoisted(() => vi.fn());

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, signIn, logout: vi.fn() }),
}));

const USER = { id: "u-1", email: "pilot@example.com", name: "Pilot" };

function answer(status: number, body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      })
  );
}

function openLink(token = "tok-123", { isAdmin = false } = {}) {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[`/auth/link/${token}`]}>
        <Routes>
          <Route path="/auth/link/:token" element={<AuthLinkPage isAdmin={isAdmin} />} />
          <Route path="/" element={<div>Dashboard</div>} />
        </Routes>
      </MemoryRouter>
    </StrictMode>
  );
}

describe("the sign-in link page", () => {
  beforeEach(() => {
    signIn.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("redeems the token exactly once", async () => {
    vi.stubGlobal("fetch", answer(200, { user: USER }));
    openLink("tok-abc");

    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    expect(fetch).toHaveBeenCalledTimes(1);
    const [path, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(path).toBe("/api/auth/magic-link/consume");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ token: "tok-abc" });
  });

  it("adopts the identity the redemption answered with, then routes to the app", async () => {
    vi.stubGlobal("fetch", answer(200, { user: USER }));
    openLink();

    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    expect(signIn).toHaveBeenCalledWith(USER);
  });

  it("does not call a live link dead when the server could not be reached", async () => {
    // A 400 is the server's final answer about the token. A 5xx or a dropped
    // connection says nothing about it, and sending the person off to request
    // another throws away a link that still works.
    vi.stubGlobal("fetch", answer(503, { detail: "upstream is unwell" }));
    openLink();

    expect(await screen.findByText(/could not reach the server/i)).toBeInTheDocument();
    expect(screen.queryByText("That sign-in link is no longer valid")).not.toBeInTheDocument();
    expect(screen.getByText(/has not been used/i)).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it("retries the same token after a transient failure", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("{}", { status: 500 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ user: USER }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchMock);
    openLink("tok-retry");

    const retry = await screen.findByRole("button", { name: /try again/i });
    retry.click();

    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ token: "tok-retry" });
  });

  it("shows the login card with one message when the link is spent", async () => {
    vi.stubGlobal("fetch", answer(400, { detail: "That sign-in link is no longer valid" }));
    openLink();

    expect(await screen.findByText("That sign-in link is no longer valid")).toBeInTheDocument();
    // The login card, so the failure ends somewhere they can ask for another.
    expect(screen.getByText("Retina")).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    expect(signIn).not.toHaveBeenCalled();
  });

  it.each([
    ["a spent link", 400, "That sign-in link is no longer valid", false, 1],
    ["a spent link", 400, "That sign-in link is no longer valid", true, 0],
    ["an unreachable server", 503, "We could not reach the server to sign you in.", false, 1],
    ["an unreachable server", 503, "We could not reach the server to sign you in.", true, 0],
  ])("offers the map after %s unless on the admin console (%i, admin: %s)", async (_case, status, shown, isAdmin, offered) => {
    vi.stubGlobal("fetch", answer(status, { detail: "whatever the server said" }));
    openLink("tok-123", { isAdmin });

    await screen.findByText(shown);
    expect(screen.queryAllByRole("button", { name: "Back to the map" })).toHaveLength(offered);
  });
});
