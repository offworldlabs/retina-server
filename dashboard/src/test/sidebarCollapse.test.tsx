import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DashboardLayout from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";

const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" } as { name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

// Sidebar and Header both call useAuth, which throws outside AuthProvider; the
// house pattern (signedOutChrome.test.tsx) mocks it rather than mounting one.
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

/** Stubbed rather than borrowed, as in theme.test.tsx: jsdom has no matchMedia,
 *  and Node 20 and 26 disagree about whose window.localStorage is reached. */
function stubBrowser(seed: Record<string, string> = {}) {
  const store = new Map(Object.entries(seed));
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: (i: number) => [...store.keys()][i] ?? null,
      get length() { return store.size; },
    },
  });
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
  return store;
}

function renderAt(path: string) {
  // ThemeProvider because DashboardLayout renders Header, which calls useTheme.
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <DashboardLayout isAdmin={false}>
          <div>page</div>
        </DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("sidebar collapse", () => {
  beforeEach(() => stubBrowser());

  it("starts expanded on an ordinary page", () => {
    const { container } = renderAt("/");
    expect(container.querySelector(".dashboard.sidebar-collapsed")).toBeNull();
  });

  it("starts collapsed on the map route", () => {
    const { container } = renderAt("/map");
    expect(container.querySelector(".dashboard.sidebar-collapsed")).not.toBeNull();
  });

  it("toggles and remembers the choice", () => {
    const store = stubBrowser();
    const { container } = renderAt("/");
    fireEvent.click(screen.getByRole("button", { name: /collapse sidebar/i }));
    expect(container.querySelector(".dashboard.sidebar-collapsed")).not.toBeNull();
    expect(store.get("retina.sidebarCollapsed")).toBe("true");
  });

  it("an explicit choice beats the map route's default", () => {
    stubBrowser({ "retina.sidebarCollapsed": "false" });
    const { container } = renderAt("/map");
    expect(container.querySelector(".dashboard.sidebar-collapsed")).toBeNull();
  });

  it("survives storage that throws", () => {
    stubBrowser();
    const boom = () => { throw new Error("storage unavailable"); };
    Object.defineProperty(window, "localStorage", {
      value: { getItem: boom, setItem: boom, removeItem: boom, clear: boom, key: boom, length: 0 },
      configurable: true,
      writable: true,
    });
    expect(() => renderAt("/")).not.toThrow();
  });
});
