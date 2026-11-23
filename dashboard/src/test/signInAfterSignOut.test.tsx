import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "../context/AuthContext";
import AuthLinkPage from "../pages/AuthLinkPage";
import ClaimPage from "../pages/ClaimPage";

/**
 * A sign-in redeemed while a sign-out is still in flight.
 *
 * The sign-out's answer clears the session cookie, whichever session the
 * browser holds when it lands, so a sign-in link or a claim redeemed alongside
 * it could open a session only for that answer to end it. The real provider
 * and pages are used, with fetch stubbed as the server and the browser's cookie
 * jar, which each answer changes as it lands.
 */

const ADA = { id: "u1", name: "Ada", email: "ada@example.com", role: "user" };
const PILOT = { id: "u2", name: "Pilot", email: "pilot@example.com", role: "user" };
const ACCOUNTS: Record<string, typeof ADA> = { ada: ADA, pilot: PILOT };

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Ada's session, and a sign-out that answers only when the test lets it. The
 *  log says when each request left and each answer landed. */
function stubServer() {
  const browser = { cookie: "ada" as string | null, log: [] as string[] };
  let answerSignOut = () => {};
  // Both redemptions open a session for Pilot, in place of any there was.
  const redeem = (body: object) => {
    browser.log.push("redemption sent");
    browser.cookie = "pilot";
    browser.log.push("redemption answered");
    return json(200, { ...body, user: PILOT });
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const post = init.method === "POST";
      if (url === "/api/auth/me") return browser.cookie ? json(200, ACCOUNTS[browser.cookie]) : json(401, {});
      if (url === "/api/auth/logout") {
        browser.log.push("sign-out sent");
        return new Promise<Response>((resolve) => {
          answerSignOut = () => {
            // Its Set-Cookie deletes auth_token, whoever it belongs to by now.
            browser.cookie = null;
            browser.log.push("sign-out answered");
            resolve(json(200, { ok: true }));
          };
        });
      }
      if (post && url === "/api/auth/magic-link/consume") return redeem({});
      if (post && url === "/api/auth/claim/consume") return redeem({ node_ref: "node-1" });
      if (url.startsWith("/api/auth/magic-link/")) return json(200, { email: PILOT.email });
      if (url.startsWith("/api/auth/claim/")) return json(200, { node_ref: "node-1" });
      return json(404, {});
    }),
  );
  return { browser, answerSignOut: () => answerSignOut() };
}

/** A page with the session's Sign out, and the two mailed links. */
function Home() {
  const { logout } = useAuth();
  return (
    <>
      <button onClick={() => void logout()}>Sign out</button>
      <Link to="/auth/link/tok">the sign-in link</Link>
      <Link to="/auth/claim/tok">the claim link</Link>
    </>
  );
}

function Identity() {
  const { user } = useAuth();
  return <output aria-label="identity">{user ? user.name : "nobody"}</output>;
}

function renderConsole() {
  return render(
    <MemoryRouter initialEntries={["/home"]}>
      <AuthProvider>
        <Identity />
        <Routes>
          <Route path="/home" element={<Home />} />
          <Route path="/auth/link/:token" element={<AuthLinkPage />} />
          <Route path="/auth/claim/:token" element={<ClaimPage />} />
          <Route path="/" element={<h1>Dashboard</h1>} />
          <Route path="/overview" element={<h1>My nodes</h1>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a sign-in redeemed while a sign-out is in flight", () => {
  it.each([
    ["the sign-in link", `Sign in as ${PILOT.email}`],
    ["the claim link", "Yes, this is my node"],
  ])("waits for the sign-out to land, so the session %s opens survives it", async (link, press) => {
    const { browser, answerSignOut } = stubServer();
    renderConsole();
    await waitFor(() => expect(screen.getByLabelText("identity")).toHaveTextContent("Ada"));

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    fireEvent.click(screen.getByRole("link", { name: link }));
    fireEvent.click(await screen.findByRole("button", { name: press }));
    await act(async () => {});

    // Held until the sign-out has answered.
    expect(browser.log).toEqual(["sign-out sent"]);

    answerSignOut();

    await waitFor(() => expect(screen.getByLabelText("identity")).toHaveTextContent("Pilot"));
    expect(browser.log).toEqual(["sign-out sent", "sign-out answered", "redemption sent", "redemption answered"]);
    expect(browser.cookie).toBe("pilot");
  });
});
