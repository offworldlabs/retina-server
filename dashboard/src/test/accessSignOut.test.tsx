import { useState } from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { api } from "../api/client";
import { AuthProvider, useAuth } from "../context/AuthContext";

/**
 * Signing out of the admin console has to end the session Cloudflare holds.
 *
 * On the admin hostnames identity arrives as an Access assertion, from a cookie
 * on Cloudflare's own domain that this app cannot delete, so clearing local
 * state leaves a refresh fully authenticated. Only a top-level navigation to
 * the edge's logout path ends it, and the server says when that is needed: the
 * same bundle serves dash, where the session is ours and there is nothing at
 * the edge to end.
 */

const realLocation = window.location;

function stubLocation() {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    value: { pathname: "/nodes", href: "https://admin.retina.fm/nodes", assign },
    writable: true,
    configurable: true,
  });
  return assign;
}

function Probe() {
  const { user, logout } = useAuth();
  const [outcome, setOutcome] = useState("ready");
  return (
    <>
      <button
        onClick={async () => {
          const { redirected } = await logout();
          setOutcome(redirected ? "redirected" : "stayed");
        }}
      >
        {outcome}
      </button>
      <span data-testid="identity">{user ? "held" : "cleared"}</span>
    </>
  );
}

async function mountProbe() {
  vi.spyOn(api, "me").mockResolvedValue({ email: "someone@offworldlab.com" });
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>
  );
  await waitFor(() => expect(api.me).toHaveBeenCalled());
  return screen.getByRole("button");
}

describe("signing out of an Access session", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  it("sends the browser to the logout path the server named", async () => {
    const assign = stubLocation();
    vi.spyOn(api, "logout").mockResolvedValue({ ok: true, redirect: "/cdn-cgi/access/logout" });
    const button = await mountProbe();

    fireEvent.click(button);

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/cdn-cgi/access/logout"));
  });

  it("holds the identity until the navigation commits", async () => {
    // RequireAuth (App.tsx) renders <Navigate to="/login"> the moment user goes
    // falsy. Clearing it here would fire that guard against a top-level
    // navigation already in flight, so the identity stays put and the departing
    // page keeps rendering until the new document takes over.
    stubLocation();
    vi.spyOn(api, "logout").mockResolvedValue({ ok: true, redirect: "/cdn-cgi/access/logout" });
    const button = await mountProbe();

    fireEvent.click(button);

    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("redirected"));
    expect(screen.getByTestId("identity")).toHaveTextContent("held");
  });

  it("reports that it redirected, so the caller does not route over the top", async () => {
    // Routing to /login in the same tick would replace the navigation that
    // actually ends the session, leaving the original bug in place.
    stubLocation();
    vi.spyOn(api, "logout").mockResolvedValue({ ok: true, redirect: "/cdn-cgi/access/logout" });
    const button = await mountProbe();

    fireEvent.click(button);

    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("redirected"));
  });
});

describe("signing out of a session this app owns", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  it("stays in the app when the server names no logout path", async () => {
    const assign = stubLocation();
    vi.spyOn(api, "logout").mockResolvedValue({ ok: true });
    const button = await mountProbe();

    fireEvent.click(button);

    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("stayed"));
    expect(assign).not.toHaveBeenCalled();
  });

  it.each(["https://evil.example/", "//evil.example/", "javascript:alert(1)"])(
    "refuses to follow %s, which is not a path on this origin",
    async (redirect) => {
      // The value is our own constant today. The guard is what keeps a later
      // change to the field from turning sign-out into an open redirect, at
      // the one moment a user expects to be handed to an auth screen. Same
      // rule as _safe_redirect on the server.
      const assign = stubLocation();
      vi.spyOn(api, "logout").mockResolvedValue({ ok: true, redirect });
      const button = await mountProbe();

      fireEvent.click(button);

      await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("stayed"));
      expect(assign).not.toHaveBeenCalled();
    }
  );
});
