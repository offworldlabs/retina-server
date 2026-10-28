import { useEffect, useState } from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DashboardLayout from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";
import { stubMatchMedia } from "./matchMedia";

/**
 * A page's state belongs to the session that fetched it. Signing out, or in
 * as someone else, has to start each page over rather than leave the last
 * account's answer on screen, while the chrome around the page carries on.
 */

type User = { id: string; name: string; email: string };

const ada: User = { id: "u1", name: "Ada", email: "ada@example.com" };
const grace: User = { id: "u2", name: "Grace", email: "grace@example.com" };

const state = vi.hoisted(() => ({
  auth: {
    user: null as { id: string; name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

/** Asked once per mount, the way a page fetches on arrival: whoever is signed
 *  in when it asks is whose marks come back. */
const fetchMarks = vi.fn(async () => (state.auth.user ? `${state.auth.user.name}'s marks` : "no marks"));

/** A stand-in for a page like the leaderboard: it fetches on mount and holds
 *  the answer, and a choice of its own, in local state. */
function MarksPage() {
  const [marks, setMarks] = useState("loading");
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    let live = true;
    fetchMarks().then((answer) => live && setMarks(answer));
    return () => {
      live = false;
    };
  }, []);
  return (
    <div>
      <p data-testid="marks">{marks}</p>
      <button type="button" onClick={() => setExpanded(true)}>
        {expanded ? "expanded" : "expand"}
      </button>
    </div>
  );
}

function tree() {
  return (
    <ThemeProvider>
      <MemoryRouter initialEntries={["/leaderboard"]}>
        <DashboardLayout isAdmin={false}>
          <MarksPage />
        </DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>
  );
}

/** Mount the console signed in as `user`, and let the page's fetch land. */
async function mountAs(user: User | null) {
  state.auth.user = user;
  const view = render(tree());
  await screen.findByText(user ? `${user.name}'s marks` : "no marks");
  return view;
}

/** Change who is signed in, as the provider does, and let any refetch land. */
async function becomeUser(user: User | null, rerender: (ui: React.ReactElement) => void) {
  state.auth = { ...state.auth, user };
  await act(async () => rerender(tree()));
}

describe("a page under a change of identity", () => {
  beforeEach(() => {
    stubMatchMedia();
    fetchMarks.mockClear();
  });

  it("starts over when its caller signs out", async () => {
    const { container, rerender } = await mountAs(ada);
    fireEvent.click(screen.getByRole("button", { name: "expand" }));
    const header = container.querySelector(".header");
    const sidebar = container.querySelector(".sidebar");

    await becomeUser(null, rerender);

    expect(fetchMarks).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("marks")).toHaveTextContent("no marks");
    expect(screen.queryByText("Ada's marks")).toBeNull();
    // The page's own choice went with it.
    expect(screen.getByRole("button", { name: "expand" })).toBeInTheDocument();
    // The chrome is the same element, redrawn signed out rather than rebuilt.
    expect(container.querySelector(".header")).toBe(header);
    expect(container.querySelector(".sidebar")).toBe(sidebar);
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("starts over when another account signs in", async () => {
    const { container, rerender } = await mountAs(ada);
    fireEvent.click(screen.getByRole("button", { name: "expand" }));
    const header = container.querySelector(".header");

    await becomeUser(grace, rerender);

    expect(fetchMarks).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("marks")).toHaveTextContent("Grace's marks");
    expect(screen.getByRole("button", { name: "expand" })).toBeInTheDocument();
    expect(container.querySelector(".header")).toBe(header);
  });

  it("starts over when a visitor signs in", async () => {
    const { container, rerender } = await mountAs(null);
    const header = container.querySelector(".header");

    await becomeUser(ada, rerender);

    expect(fetchMarks).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("marks")).toHaveTextContent("Ada's marks");
    expect(container.querySelector(".header")).toBe(header);
  });

  // Keyed on the account, not on the object holding it, which is a new one
  // whenever the identity is set again.
  it("keeps its state across a fresh copy of the same identity", async () => {
    const { rerender } = await mountAs(ada);
    fireEvent.click(screen.getByRole("button", { name: "expand" }));

    await becomeUser({ ...ada }, rerender);

    expect(fetchMarks).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "expanded" })).toBeInTheDocument();
  });
});
