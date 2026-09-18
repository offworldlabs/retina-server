import { describe, it, expect, beforeEach, vi } from "vitest";
import { render } from "@testing-library/react";
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
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <DashboardLayout isAdmin={false}><div>page</div></DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("the map's content pane", () => {
  beforeEach(() => stubBrowser());

  it("is flush on the map route", () => {
    const { container } = renderAt("/map");
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  it("is flush on the physics route", () => {
    const { container } = renderAt("/physics");
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  it("keeps its padding everywhere else", () => {
    const { container } = renderAt("/detections");
    expect(container.querySelector(".content.flush")).toBeNull();
    expect(container.querySelector(".content")).not.toBeNull();
  });
});
