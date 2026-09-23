import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Header from "../components/Header";
import { ThemeProvider } from "../context/ThemeContext";

const state = vi.hoisted(() => ({ user: null as { name: string; email: string } | null }));

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: state.user, loading: false, logout: vi.fn(async () => ({ redirected: false })) }),
}));

/** jsdom has no matchMedia. */
function stubMatchMedia() {
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
}

function renderHeader() {
  render(
    <MemoryRouter>
      <ThemeProvider>
        <Header title="Overview" />
      </ThemeProvider>
    </MemoryRouter>,
  );
}

/** The switch sits in the bar whether or not there is a session, and must be
 *  the same control either way, so every test below runs in both states. */
const CALLERS = [
  { caller: "a caller with a session", user: { name: "Ada", email: "ada@example.com" } },
  { caller: "a caller with no session", user: null },
];

beforeEach(() => {
  stubMatchMedia();
  document.documentElement.removeAttribute("data-theme");
});

describe.each(CALLERS)("the appearance switch shown to $caller", ({ user }) => {
  beforeEach(() => {
    state.user = user;
  });

  // Two things at once, both easy to lose. The buttons hold a glyph and no
  // text, so without aria-label a screen reader reads three unlabelled radios
  // and the control becomes unusable rather than merely ugly. And the order is
  // light → system → dark, a run from one extreme to the other with the neutral
  // between them, which is a decision rather than an accident of the array.
  it("names all three settings in order, though none of them carries text", () => {
    renderHeader();
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("aria-label"))).toEqual(["Light", "System", "Dark"]);
    expect(radios.every((r) => r.textContent === "")).toBe(true);
  });

  it("offers the same words as a tooltip", () => {
    renderHeader();
    expect(screen.getAllByRole("radio").map((r) => r.getAttribute("title"))).toEqual(["Light", "System", "Dark"]);
  });

  it("hides the glyph itself from assistive tech, so the name is not read twice", () => {
    renderHeader();
    for (const r of screen.getAllByRole("radio")) {
      expect(r.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("marks the current setting, and moves the mark when another is picked", () => {
    renderHeader();
    const checked = () =>
      screen.getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true");

    expect(checked()).toHaveAttribute("aria-label", "System");
    act(() => screen.getByRole("radio", { name: "Dark" }).click());
    expect(checked()).toHaveAttribute("aria-label", "Dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});

describe("where the appearance switch sits", () => {
  it("is in the bar without opening anything, beside the avatar", () => {
    state.user = { name: "Ada", email: "ada@example.com" };
    renderHeader();
    expect(screen.getByRole("radiogroup", { name: "Appearance" })).toBeInTheDocument();

    // And not a second time in the avatar menu.
    act(() => screen.getByText("Ada").click());
    expect(screen.getByText("Sign out")).toBeInTheDocument();
    expect(screen.getAllByRole("radiogroup")).toHaveLength(1);
  });

  it("is in the bar beside Sign in for a caller with no session", () => {
    state.user = null;
    renderHeader();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Appearance" })).toBeInTheDocument();
  });
});

/**
 * The keyboard contract that comes with role="radiogroup". Choosing the radio
 * role over three independent toggle buttons is what obliges this: the set is
 * announced as one control with three options, and a keyboard user expects one
 * tab stop with the arrows moving inside it.
 */
describe.each(CALLERS)("the appearance switch by keyboard, shown to $caller", ({ user }) => {
  beforeEach(() => {
    state.user = user;
  });

  const group = () => screen.getByRole("radiogroup");
  const labels = () => screen.getAllByRole("radio").map((r) => r.getAttribute("aria-label"));
  const focused = () => document.activeElement?.getAttribute("aria-label");
  const checked = () =>
    screen
      .getAllByRole("radio")
      .find((r) => r.getAttribute("aria-checked") === "true")
      ?.getAttribute("aria-label");

  it("is one tab stop, on whichever option is checked", () => {
    renderHeader();
    const tabbable = screen.getAllByRole("radio").filter((r) => r.getAttribute("tabindex") === "0");
    expect(tabbable).toHaveLength(1);
    expect(tabbable[0]).toHaveAttribute("aria-label", checked()!);
  });

  it("moves the selection right, and takes focus with it", () => {
    renderHeader();
    expect(labels()).toEqual(["Light", "System", "Dark"]);
    expect(checked()).toBe("System");

    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" }));
    expect(checked()).toBe("Dark");
    expect(focused()).toBe("Dark");
  });

  it("moves the selection left", () => {
    renderHeader();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" }));
    expect(checked()).toBe("Light");
    expect(focused()).toBe("Light");
  });

  it("wraps at both ends rather than stopping", () => {
    renderHeader();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" })); // to Light
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" })); // wraps to Dark
    expect(checked()).toBe("Dark");

    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" })); // wraps to Light
    expect(checked()).toBe("Light");
  });

  it("takes Home and End to the ends", () => {
    renderHeader();
    act(() => void fireEvent.keyDown(group(), { key: "End" }));
    expect(checked()).toBe("Dark");
    act(() => void fireEvent.keyDown(group(), { key: "Home" }));
    expect(checked()).toBe("Light");
  });

  // Without preventDefault the arrows scroll the dropdown and Home/End jump the
  // page, while the selection moves underneath. Everything else must pass.
  it("swallows only the keys it handles", () => {
    renderHeader();
    expect(fireEvent.keyDown(group(), { key: "ArrowRight" })).toBe(false);
    expect(fireEvent.keyDown(group(), { key: "a" })).toBe(true);
  });

  it("applies an arrow selection to the document, not just the markup", () => {
    renderHeader();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
