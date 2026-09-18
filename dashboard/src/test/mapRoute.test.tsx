import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Sidebar from "../components/Sidebar";
import { PUBLIC_ROUTES } from "../utils/publicRoutes";

const state = vi.hoisted(() => ({
  auth: {
    user: null as { name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const ada = { name: "Ada", email: "ada@example.com" };

function renderSidebar(isAdmin = false) {
  render(
    <MemoryRouter>
      <Sidebar isAdmin={isAdmin} collapsed={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

describe("the map is a page, not a link out", () => {
  beforeEach(() => {
    state.auth = { user: null, loading: false, logout: async () => ({ redirected: false }) };
  });

  it("is an internal route for a signed-in user", () => {
    state.auth.user = ada;
    renderSidebar();
    const map = screen.getByRole("link", { name: /^map$/i });
    expect(map).toHaveAttribute("href", "/map");
    expect(map).not.toHaveAttribute("target");
  });

  it("is an internal route for a visitor with no session", () => {
    renderSidebar();
    const map = screen.getByRole("link", { name: /^map$/i });
    expect(map).toHaveAttribute("href", "/map");
    expect(map).not.toHaveAttribute("target");
  });

  it("is listed as open, and no longer as another bundle's", () => {
    const entry = PUBLIC_ROUTES.find((r) => r.path === "/map");
    expect(entry).toBeDefined();
    expect(entry).not.toHaveProperty("external");
  });

  it("is absent from the admin console", () => {
    state.auth.user = ada;
    renderSidebar(true);
    expect(screen.queryByRole("link", { name: /^map$/i })).toBeNull();
  });
});
