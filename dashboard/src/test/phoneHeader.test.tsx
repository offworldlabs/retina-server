import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, within, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DashboardLayout from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";
import { stubViewport } from "./matchMedia";

/* A phone's header has room for three things: Menu, the page's title and the
   account. The appearance switch goes to the foot of the drawer that Menu
   opens, and everywhere else stays in the bar as before. */

const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" },
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const APPEARANCE = { name: "Appearance" };

function renderShell({ nav = true } = {}) {
  const { container } = render(
    <ThemeProvider>
      <MemoryRouter initialEntries={["/overview"]}>
        <DashboardLayout isAdmin={false} nav={nav}>
          <div>page</div>
        </DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
  return {
    header: container.querySelector<HTMLElement>("header.header")!,
    sidebar: container.querySelector<HTMLElement>("#console-sidebar")!,
  };
}

beforeEach(() => document.documentElement.removeAttribute("data-theme"));
afterEach(() => vi.unstubAllGlobals());

describe("the header on a phone", () => {
  it("holds Menu, the title and the account, and leaves the appearance switch to the drawer's foot", () => {
    stubViewport(390);
    const { header, sidebar } = renderShell();
    const bar = within(header);
    expect(bar.getByRole("button", { name: "Menu" })).toBeInTheDocument();
    expect(header.querySelector(".header-title")).toHaveTextContent("My Nodes");
    expect(bar.getByRole("button", { name: "Account: Ada" })).toBeInTheDocument();
    expect(bar.queryByRole("radiogroup", APPEARANCE)).toBeNull();

    // After the nav, so Tab reaches it last.
    const footer = within(sidebar).getByRole("radiogroup", APPEARANCE).closest(".sidebar-footer");
    expect(footer?.previousElementSibling).toHaveClass("sidebar-nav");
  });

  it("applies a theme chosen at the drawer's foot", () => {
    stubViewport(390);
    const { sidebar } = renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    fireEvent.click(within(sidebar).getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(within(sidebar).getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "true");
  });

  // Without Menu the drawer never opens, and a switch put there would be out
  // of reach.
  it("keeps the switch in the bar when there is no Menu to open the drawer", () => {
    stubViewport(390);
    const { header, sidebar } = renderShell({ nav: false });
    expect(within(header).queryByRole("button", { name: "Menu" })).toBeNull();
    expect(within(header).getByRole("radiogroup", APPEARANCE)).toBeInTheDocument();
    expect(within(sidebar).queryByRole("radiogroup", APPEARANCE)).toBeNull();
  });

  it("takes the switch back into the bar when the phone turns past the phone width", () => {
    const viewport = stubViewport(390);
    const { header, sidebar } = renderShell();
    viewport.resize(844);
    expect(within(header).getByRole("radiogroup", APPEARANCE)).toBeInTheDocument();
    expect(sidebar.querySelector(".sidebar-footer")).toBeNull();
    viewport.resize(390);
    expect(within(header).queryByRole("radiogroup", APPEARANCE)).toBeNull();
    expect(within(sidebar).getByRole("radiogroup", APPEARANCE)).toBeInTheDocument();
  });
});

describe.each([768, 1440])("the header at %ipx", (width) => {
  it("keeps the appearance switch, and the sidebar has no footer", () => {
    stubViewport(width);
    const { header, sidebar } = renderShell();
    expect(within(header).getByRole("radiogroup", APPEARANCE)).toBeInTheDocument();
    expect(sidebar.querySelector(".sidebar-footer")).toBeNull();
  });
});
