import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import DashboardLayout from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";
import rules from "../App.css?raw";
import { stubMatchMedia } from "./matchMedia";

// Comments name selectors in prose, and the sweeps below would read them.
const sheet = rules.replace(/\/\*[\s\S]*?\*\//g, "");

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

function renderAt(path: string, page: React.ReactNode = <div>page</div>) {
  // ThemeProvider because DashboardLayout renders Header, which calls useTheme.
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <DashboardLayout isAdmin={false}>{page}</DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("sidebar collapse", () => {
  beforeEach(() => stubMatchMedia());

  it("starts expanded on an ordinary page", () => {
    const { container } = renderAt("/");
    expect(container.querySelector(".dashboard.sidebar-collapsed")).toBeNull();
  });

  it("starts collapsed on the map route", () => {
    const { container } = renderAt("/map");
    expect(container.querySelector(".dashboard.sidebar-collapsed")).not.toBeNull();
  });

  it("toggles and remembers the choice", () => {
    const { container } = renderAt("/");
    fireEvent.click(screen.getByRole("button", { name: /collapse sidebar/i }));
    expect(container.querySelector(".dashboard.sidebar-collapsed")).not.toBeNull();
    expect(window.localStorage.getItem("retina.sidebarCollapsed")).toBe("true");
  });

  it("an explicit choice beats the map route's default", () => {
    window.localStorage.setItem("retina.sidebarCollapsed", "false");
    const { container } = renderAt("/map");
    expect(container.querySelector(".dashboard.sidebar-collapsed")).toBeNull();
  });

  it("survives storage that throws", () => {
    const boom = () => { throw new Error("storage unavailable"); };
    Object.defineProperty(window, "localStorage", {
      value: { getItem: boom, setItem: boom, removeItem: boom, clear: boom, key: boom, length: 0 },
      configurable: true,
      writable: true,
    });
    expect(() => renderAt("/")).not.toThrow();
  });
});

/** Below the breakpoint the sidebar leaves the row and waits off-canvas, and
 *  the header's Menu button brings it over the page. jsdom applies no media
 *  query, so what is asserted here is the state the stylesheet keys off. */
describe("the sidebar as a drawer on a narrow screen", () => {
  beforeEach(() => stubMatchMedia());

  const menuButton = () => screen.getByRole("button", { name: "Menu" });
  const isOpen = (container: HTMLElement) =>
    container.querySelector(".dashboard.menu-open") !== null;

  it("opens from the header, which says so", () => {
    const { container } = renderAt("/overview");
    expect(menuButton()).toHaveAttribute("aria-controls", "console-sidebar");
    expect(menuButton()).toHaveAttribute("aria-expanded", "false");
    expect(isOpen(container)).toBe(false);

    fireEvent.click(menuButton());
    expect(isOpen(container)).toBe(true);
    expect(menuButton()).toHaveAttribute("aria-expanded", "true");
  });

  it("takes focus to its first entry, which comes before the header in the tab order", () => {
    renderAt("/overview");
    fireEvent.click(menuButton());
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Overview" }));
  });

  // Tab past the header would otherwise walk the page under the scrim.
  it("takes the page it covers out of reach until it closes", () => {
    const { container } = renderAt("/overview");
    const page = container.querySelector<HTMLElement>(".content")!;
    fireEvent.click(menuButton());
    expect(page.inert).toBe(true);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(page.inert).toBe(false);
  });

  // Otherwise the wide layout would keep an inert page and no visible Menu.
  it("closes when the screen widens past the breakpoint", () => {
    let onChange: () => void = () => {};
    const query = { matches: true, addEventListener: (_: string, f: () => void) => void (onChange = f), removeEventListener: () => {} };
    const theme = window.matchMedia("(prefers-color-scheme: dark)");
    window.matchMedia = vi.fn((q: string) => (q === "(max-width: 768px)" ? query : theme)) as unknown as typeof window.matchMedia;
    const { container } = renderAt("/overview");
    fireEvent.click(menuButton());
    query.matches = false;
    act(() => onChange());
    expect(isOpen(container)).toBe(false);
    expect(container.querySelector<HTMLElement>(".content")!.inert).toBe(false);
  });

  it("closes on Escape and hands focus back to the button", () => {
    const { container } = renderAt("/overview");
    fireEvent.click(menuButton());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(isOpen(container)).toBe(false);
    expect(document.activeElement).toBe(menuButton());
  });

  it("closes when the page beside it is tapped", () => {
    const { container } = renderAt("/overview");
    fireEvent.click(menuButton());
    fireEvent.click(container.querySelector(".sidebar-scrim")!);
    expect(isOpen(container)).toBe(false);
  });

  it("closes on choosing another page", () => {
    const { container } = renderAt("/overview");
    fireEvent.click(menuButton());
    fireEvent.click(screen.getByRole("link", { name: "Detections" }));
    expect(isOpen(container)).toBe(false);
  });

  // The path does not change, so only the click itself can close it.
  it("closes on choosing the page already open", () => {
    const { container } = renderAt("/overview");
    fireEvent.click(menuButton());
    fireEvent.click(screen.getByRole("link", { name: "Overview" }));
    expect(isOpen(container)).toBe(false);
  });

  // Back and forward move the page without touching the menu.
  it("closes when the page changes by any other route", () => {
    function Elsewhere() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/rf")}>elsewhere</button>;
    }
    const { container } = renderAt("/overview", <Elsewhere />);
    fireEvent.click(menuButton());
    fireEvent.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(isOpen(container)).toBe(false);
  });

  // The rail is a wide-screen shape: the drawer always carries its labels, and
  // its collapse control would only change a setting the narrow screen ignores.
  it("keeps the collapsed rail to wide screens", () => {
    const wide = sheet.match(/@media \(min-width: 769px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";
    const outside = sheet.replace(wide, "");
    expect(wide).toMatch(/\.dashboard\.sidebar-collapsed \.sidebar \{/);
    expect(outside).not.toMatch(/\.sidebar-collapsed/);
  });

  // A link inherits the drawer's visibility, and under `transition: all` the
  // change from hidden starts hidden, so the focus sent on opening is refused.
  it("lets its entries take focus the moment it opens", () => {
    const entry = sheet.match(/\n\.nav-item \{([^}]*)\}/)?.[1];
    expect(entry).toBeDefined();
    expect(entry).not.toMatch(/transition:[^;]*\b(all|visibility)\b/);
  });

  it("is never taken out of the page outright", () => {
    expect(sheet).not.toMatch(/\.sidebar\s*\{[^}]*display:\s*none/);
  });
});
