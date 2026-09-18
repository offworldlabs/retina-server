import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import ClaimPage from "../pages/ClaimPage";

const signIn = vi.hoisted(() => vi.fn());

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: null, loading: false, signIn, logout: vi.fn() }),
}));

vi.mock("../api/client", () => ({
  api: {
    claimPreview: vi.fn(),
    consumeClaim: vi.fn(),
    declineClaim: vi.fn(),
  },
}));

function renderAt(token = "tok") {
  return render(
    <MemoryRouter initialEntries={[`/auth/claim/${token}`]}>
      <Routes>
        <Route path="/auth/claim/:token" element={<ClaimPage />} />
        <Route path="/" element={<div>dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ClaimPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.claimPreview).mockResolvedValue({ node_ref: "nde4f2k9xq7m3b8" });
  });

  it("names the node before either button is pressed", async () => {
    renderAt();

    expect(await screen.findByText(/nde4f2k9xq7m3b8/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /this is my node/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /did not ask/i })).toBeInTheDocument();
  });

  it("does not spend the link to name the node", async () => {
    renderAt();

    await screen.findByText(/nde4f2k9xq7m3b8/);
    expect(api.consumeClaim).not.toHaveBeenCalled();
    expect(api.declineClaim).not.toHaveBeenCalled();
  });

  it("binds the node on confirm", async () => {
    vi.mocked(api.consumeClaim).mockResolvedValue({ node_ref: "nde4f2k9xq7m3b8", user: {} });
    renderAt("abc");
    await screen.findByText(/nde4f2k9xq7m3b8/);

    fireEvent.click(screen.getByRole("button", { name: /this is my node/i }));

    expect(api.consumeClaim).toHaveBeenCalledWith("abc");
    expect(await screen.findByText(/is yours/i)).toBeInTheDocument();
  });

  it("signs the browser in as the account the click created", async () => {
    // The session cookie alone is not enough: the app read its identity once,
    // at load, and would send a fresh owner to the login card.
    const user = { id: "u-1", email: "ada@example.com" };
    vi.mocked(api.consumeClaim).mockResolvedValue({ node_ref: "nde4f2k9xq7m3b8", user });
    renderAt("abc");
    await screen.findByText(/nde4f2k9xq7m3b8/);

    fireEvent.click(screen.getByRole("button", { name: /this is my node/i }));

    await waitFor(() => expect(signIn).toHaveBeenCalledWith(user));
    expect(await screen.findByText("dashboard", {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it("tells a stranger nothing happened when they decline", async () => {
    vi.mocked(api.declineClaim).mockResolvedValue({ ok: true });
    renderAt("abc");
    await screen.findByText(/nde4f2k9xq7m3b8/);

    fireEvent.click(screen.getByRole("button", { name: /did not ask/i }));

    expect(api.declineClaim).toHaveBeenCalledWith("abc");
    expect(await screen.findByText(/not connected to this address/i)).toBeInTheDocument();
  });

  it("distinguishes a node claimed meanwhile from a link that never worked", async () => {
    vi.mocked(api.consumeClaim).mockRejectedValue(Object.assign(new Error("taken"), { status: 409 }));
    renderAt();
    await screen.findByText(/nde4f2k9xq7m3b8/);

    fireEvent.click(screen.getByRole("button", { name: /this is my node/i }));

    expect(await screen.findByText(/already belongs to someone else/i)).toBeInTheDocument();
  });

  it("offers signing in when the link is spent, rather than a dead end", async () => {
    vi.mocked(api.claimPreview).mockRejectedValue(Object.assign(new Error("gone"), { status: 404 }));

    renderAt();

    expect(await screen.findByText(/no longer valid/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in instead/i })).toBeInTheDocument();
  });

  it("offers no buttons once the answer is terminal", async () => {
    vi.mocked(api.declineClaim).mockResolvedValue({ ok: true });
    renderAt();
    await screen.findByText(/nde4f2k9xq7m3b8/);

    fireEvent.click(screen.getByRole("button", { name: /did not ask/i }));

    await waitFor(() => expect(screen.queryByRole("button")).not.toBeInTheDocument());
  });
});
