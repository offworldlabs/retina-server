import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { Link, MemoryRouter, useLocation } from "react-router-dom";

import Header, { menuFocusTarget } from "../components/Header";
import { ThemeProvider } from "../context/ThemeContext";
import { stubMatchMedia } from "./matchMedia";

/**
 * The account menu, as the WAI-ARIA APG's menu button: a button that names the
 * account and the menu it opens, a popup whose items the arrows walk while Tab
 * leaves, and focus handed back to the button when the popup is dismissed.
 * The pieces of it a mouse never exercises are the ones that go missing, so
 * each key has its own test.
 */

const state = vi.hoisted(() => ({
  auth: {
    user: null as Record<string, unknown> | null,
    signingOut: false,
    logout: (() => Promise.resolve({ redirected: false, failure: null })) as (leave?: () => void) => Promise<unknown>,
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const ADA = { id: "u1", name: "Ada", email: "ada@example.com" };

function Where() {
  return <output aria-label="location">{useLocation().pathname}</output>;
}

/** The header on the app console, or on the admin console with `isAdmin`. */
function renderHeader({ isAdmin = false, at = "/overview" } = {}) {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <ThemeProvider>
        <Header title="My Nodes" isAdmin={isAdmin} />
        <p>The page below</p>
        <Link to="/data">Open data</Link>
        <Where />
      </ThemeProvider>
    </MemoryRouter>,
  );
}

const trigger = () => screen.getByRole("button", { name: "Account: Ada" });
// Hidden while shut, which getByRole honours, so these find the menu only open.
const menu = () => screen.getByRole("menu");
const isOpen = () => screen.queryByRole("menu") !== null;
const items = () => within(menu()).getAllByRole("menuitem");
const lastItem = () => items()[items().length - 1];
const accountItem = () => within(menu()).getByRole("menuitem", { name: "Account" });
const signOutItem = () => within(menu()).getByRole("menuitem", { name: "Sign out" });
const where = () => screen.getByLabelText("location");

beforeEach(() => {
  stubMatchMedia();
  state.auth = {
    user: ADA,
    signingOut: false,
    logout: vi.fn(() => Promise.resolve({ redirected: false, failure: null })),
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the account menu's button", () => {
  it("is a button that says it opens a menu, and which one", () => {
    renderHeader();
    const button = trigger();
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");
    // Named while shut too: the popup stays in the document, hidden.
    const controlled = document.getElementById(button.getAttribute("aria-controls") ?? "");
    expect(controlled).toHaveAttribute("role", "menu");
  });

  it("names the account by its email when it has no name", () => {
    state.auth.user = { ...ADA, name: "" };
    renderHeader();
    expect(screen.getByRole("button", { name: "Account: ada@example.com" })).toBeInTheDocument();
  });

  it("shows an initial for assistive tech to skip, styled by its class rather than inline", () => {
    renderHeader();
    const initial = trigger().querySelector(".user-initial");
    expect(initial).toHaveTextContent("A");
    expect(initial).toHaveAttribute("aria-hidden", "true");
    expect(initial).not.toHaveAttribute("style");
  });

  // An older server still sends both. Nothing reads them, whichever of the two
  // changes ships first.
  it("draws no picture, even from a server that still sends one", () => {
    state.auth.user = { ...ADA, avatar: "https://example.com/ada.png", provider: "google" };
    const { container } = renderHeader();
    fireEvent.click(trigger());
    expect(container.querySelector("img")).toBeNull();
  });

  it("opens on a click and shuts on the next", () => {
    renderHeader();
    fireEvent.click(trigger());
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menu", { name: "Account: Ada" })).toBeVisible();

    fireEvent.click(trigger());
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    expect(isOpen()).toBe(false);
  });
});

describe("opening the account menu by keyboard", () => {
  // Refused as well as handled: left to the browser, Enter and Space would
  // also click the button and toggle the menu straight back shut.
  it.each(["Enter", " ", "ArrowDown"])("opens on %j with the first item focused", (key) => {
    renderHeader();
    expect(fireEvent.keyDown(trigger(), { key })).toBe(false);
    expect(trigger()).toHaveAttribute("aria-expanded", "true");
    expect(items()[0]).toHaveFocus();
  });

  it("opens on ArrowUp with the last item focused", () => {
    renderHeader();
    expect(fireEvent.keyDown(trigger(), { key: "ArrowUp" })).toBe(false);
    expect(lastItem()).toHaveFocus();
  });

  it("leaves every other key to the browser", () => {
    renderHeader();
    expect(fireEvent.keyDown(trigger(), { key: "a" })).toBe(true);
    expect(isOpen()).toBe(false);
  });
});

describe("moving through the account menu", () => {
  // Checked twice: on the real menu, Account then Sign out on the app console,
  // and on the step itself over three items.
  it("wraps the arrows at both ends", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "ArrowDown" });
    const [first] = items();
    const last = lastItem();
    expect(items()).toHaveLength(2);

    expect(fireEvent.keyDown(first, { key: "ArrowDown" })).toBe(false);
    expect(last).toHaveFocus();
    expect(fireEvent.keyDown(last, { key: "ArrowDown" })).toBe(false);
    expect(first).toHaveFocus();
    expect(fireEvent.keyDown(first, { key: "ArrowUp" })).toBe(false);
    expect(last).toHaveFocus();
    expect(fireEvent.keyDown(last, { key: "ArrowUp" })).toBe(false);
    expect(first).toHaveFocus();

    expect(menuFocusTarget("ArrowDown", 0, 3)).toBe(1);
    expect(menuFocusTarget("ArrowDown", 2, 3)).toBe(0);
    expect(menuFocusTarget("ArrowUp", 1, 3)).toBe(0);
    expect(menuFocusTarget("ArrowUp", 0, 3)).toBe(2);
  });

  it("takes Home and End to the ends", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "ArrowUp" });
    expect(fireEvent.keyDown(lastItem(), { key: "Home" })).toBe(false);
    expect(items()[0]).toHaveFocus();
    expect(fireEvent.keyDown(items()[0], { key: "End" })).toBe(false);
    expect(lastItem()).toHaveFocus();

    expect(menuFocusTarget("Home", 2, 3)).toBe(0);
    expect(menuFocusTarget("End", 0, 3)).toBe(2);
  });

  it("starts from the matching end when no item has focus yet", () => {
    expect(menuFocusTarget("ArrowDown", -1, 3)).toBe(0);
    expect(menuFocusTarget("ArrowUp", -1, 3)).toBe(2);
  });

  it("leaves other keys alone, and an empty menu with nowhere to go", () => {
    expect(menuFocusTarget("a", 0, 3)).toBeNull();
    expect(menuFocusTarget("ArrowLeft", 0, 3)).toBeNull();
    expect(menuFocusTarget("ArrowDown", -1, 0)).toBeNull();
  });
});

describe("closing the account menu", () => {
  it("shuts on Escape and hands focus back to the button", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "Enter" });
    // The narrow screen's drawer shuts on an Escape that reaches the document.
    const atDocument = vi.fn();
    document.addEventListener("keydown", atDocument);
    try {
      expect(fireEvent.keyDown(items()[0], { key: "Escape" })).toBe(false);
    } finally {
      document.removeEventListener("keydown", atDocument);
    }
    expect(isOpen()).toBe(false);
    expect(trigger()).toHaveAttribute("aria-expanded", "false");
    expect(trigger()).toHaveFocus();
    expect(atDocument).not.toHaveBeenCalled();
  });

  it("shuts on Escape pressed on the button itself", () => {
    renderHeader();
    fireEvent.click(trigger());
    trigger().focus();
    expect(fireEvent.keyDown(trigger(), { key: "Escape" })).toBe(false);
    expect(isOpen()).toBe(false);
  });

  // Not refused, and focus not moved: the browser takes it on to the next
  // control as it would from anywhere else.
  it("shuts on Tab and lets focus move on", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "Enter" });
    const item = items()[0];
    expect(fireEvent.keyDown(item, { key: "Tab" })).toBe(true);
    expect(isOpen()).toBe(false);
    expect(trigger()).not.toHaveFocus();
  });

  it.each(["mouseDown", "pointerDown"] as const)("shuts on a %s outside it", (press) => {
    renderHeader();
    fireEvent.click(trigger());
    fireEvent[press](screen.getByText("The page below"));
    expect(isOpen()).toBe(false);
  });

  it("stays open for a press inside it", () => {
    renderHeader();
    fireEvent.click(trigger());
    fireEvent.mouseDown(screen.getByText("ada@example.com"));
    fireEvent.pointerDown(screen.getByText("ada@example.com"));
    expect(isOpen()).toBe(true);
  });

  // A click alone, with no press before it, so it is the route that shuts it.
  it("shuts when the page changes", () => {
    renderHeader();
    fireEvent.click(trigger());
    fireEvent.click(screen.getByRole("link", { name: "Open data" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/data");
    expect(isOpen()).toBe(false);
  });
});

describe("the account menu's contents", () => {
  it("gives the email as text, not as a control", () => {
    renderHeader();
    fireEvent.click(trigger());
    const email = screen.getByText("ada@example.com");
    expect(email.closest("button, [role='menuitem'], [role='menu'], [disabled]")).toBeNull();
    expect(screen.queryByRole("button", { name: /ada@example\.com/ })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /ada@example\.com/ })).toBeNull();
    // Still heard: it describes the menu it sits above.
    expect(menu()).toHaveAccessibleDescription("Signed in as ada@example.com");
  });

  it("offers its actions as menu items, outside the tab order", () => {
    renderHeader();
    fireEvent.click(trigger());
    expect(items().map((item) => item.textContent)).toEqual(["Account", "Sign out"]);
    for (const item of items()) expect(item).toHaveAttribute("tabindex", "-1");
  });

  // The account page is the app console's alone.
  it("offers Sign out alone on the admin console", () => {
    renderHeader({ isAdmin: true });
    fireEvent.click(trigger());
    expect(items().map((item) => item.textContent)).toEqual(["Sign out"]);
    expect(screen.queryByRole("menuitem", { name: "Account" })).toBeNull();
    expect(document.querySelector('a[href="/account"]')).toBeNull();
  });
});

describe("Account in the account menu", () => {
  it("comes before Sign out, as a link to the account page", () => {
    renderHeader();
    fireEvent.click(trigger());
    const item = accountItem();
    expect(items()[0]).toBe(item);
    expect(item.tagName).toBe("A");
    expect(item).toHaveAttribute("href", "/account");
    expect(item).toHaveAttribute("tabindex", "-1");
  });

  it.each([
    ["a click", (item: HTMLElement) => fireEvent.click(item)],
    ["Enter", (item: HTMLElement) => fireEvent.keyDown(item, { key: "Enter" })],
    ["Space", (item: HTMLElement) => fireEvent.keyDown(item, { key: " " })],
  ])("opens the account page on %s, shut, with focus back on the button", (_how, activate) => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "ArrowDown" });
    expect(accountItem()).toHaveFocus();
    activate(accountItem());
    expect(where()).toHaveTextContent("/account");
    expect(isOpen()).toBe(false);
    expect(trigger()).toHaveFocus();
    expect(state.auth.logout).not.toHaveBeenCalled();
  });

  // The route does not change, which is what shuts it for any other link.
  it("shuts when the account page is already the one showing", () => {
    renderHeader({ at: "/account" });
    fireEvent.click(trigger());
    fireEvent.click(accountItem());
    expect(where()).toHaveTextContent("/account");
    expect(isOpen()).toBe(false);
  });

  it("refuses Enter and Space for itself, so the page does not scroll", () => {
    renderHeader();
    fireEvent.click(trigger());
    expect(fireEvent.keyDown(accountItem(), { key: " " })).toBe(false);
  });

  it("is reached by the arrows, Home and End, from either end", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "ArrowUp" });
    expect(signOutItem()).toHaveFocus();
    fireEvent.keyDown(signOutItem(), { key: "ArrowUp" });
    expect(accountItem()).toHaveFocus();
    fireEvent.keyDown(accountItem(), { key: "End" });
    expect(signOutItem()).toHaveFocus();
    fireEvent.keyDown(signOutItem(), { key: "Home" });
    expect(accountItem()).toHaveFocus();
  });

  it("shuts on Escape and hands focus back to the button", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "Enter" });
    expect(fireEvent.keyDown(accountItem(), { key: "Escape" })).toBe(false);
    expect(isOpen()).toBe(false);
    expect(trigger()).toHaveFocus();
    expect(where()).toHaveTextContent("/overview");
  });

  // Holding Enter on the button opens the menu with this item focused, and the
  // repeats land on it.
  it("ignores a held Enter repeating onto it", () => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "Enter" });
    expect(accountItem()).toHaveFocus();
    fireEvent.keyDown(accountItem(), { key: "Enter", repeat: true });
    expect(where()).toHaveTextContent("/overview");
    expect(isOpen()).toBe(true);
  });
});

describe("Sign out in the account menu", () => {
  it.each([
    ["a click", (item: HTMLElement) => fireEvent.click(item)],
    ["Enter", (item: HTMLElement) => fireEvent.keyDown(item, { key: "Enter" })],
    ["Space", (item: HTMLElement) => fireEvent.keyDown(item, { key: " " })],
  ])("signs out on %s", (_how, activate) => {
    renderHeader();
    fireEvent.keyDown(trigger(), { key: "ArrowUp" });
    expect(signOutItem()).toHaveFocus();
    activate(signOutItem());
    expect(state.auth.logout).toHaveBeenCalledTimes(1);
    expect(state.auth.logout).toHaveBeenCalledWith(expect.any(Function));
  });

  // Holding Enter on the button opens the menu, and the repeats then land on
  // the item it focused: Sign out, where it is the only item.
  it("ignores a held Enter repeating onto it", () => {
    renderHeader({ isAdmin: true });
    fireEvent.keyDown(trigger(), { key: "Enter" });
    expect(signOutItem()).toHaveFocus();
    fireEvent.keyDown(signOutItem(), { key: "Enter", repeat: true });
    expect(state.auth.logout).not.toHaveBeenCalled();
  });

  it("says a sign-out is in flight, and ignores another go at it", () => {
    state.auth.signingOut = true;
    renderHeader();
    fireEvent.click(trigger());
    const item = screen.getByRole("menuitem", { name: "Signing out…" });
    expect(item).toHaveAttribute("aria-disabled", "true");

    fireEvent.click(item);
    fireEvent.keyDown(item, { key: "Enter" });
    expect(state.auth.logout).not.toHaveBeenCalled();
  });
});
