import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Header from "../components/Header";
import { ThemeProvider } from "../context/ThemeContext";

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { name: "Ada", email: "ada@example.com" }, loading: false, logout: vi.fn() }),
}));

/** As in theme.test.tsx: stubbed rather than borrowed, because jsdom has no
 *  matchMedia and Node 20 and 26 disagree about window.localStorage. */
function stubBrowser() {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: () => null,
      length: 0,
    },
  });
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
}

/** Open the avatar menu the switch lives inside. */
function openMenu() {
  render(
    <MemoryRouter>
      <ThemeProvider>
        <Header title="Overview" />
      </ThemeProvider>
    </MemoryRouter>,
  );
  act(() => screen.getByText("Ada").click());
}

beforeEach(() => {
  stubBrowser();
  document.documentElement.removeAttribute("data-theme");
});

describe("the appearance switch", () => {
  // Two things at once, both easy to lose. The buttons hold a glyph and no
  // text, so without aria-label a screen reader reads three unlabelled radios
  // and the control becomes unusable rather than merely ugly. And the order is
  // light → system → dark, a run from one extreme to the other with the neutral
  // between them, which is a decision rather than an accident of the array.
  it("names all three settings in order, though none of them carries text", () => {
    openMenu();
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("aria-label"))).toEqual(["Light", "System", "Dark"]);
    expect(radios.every((r) => r.textContent === "")).toBe(true);
  });

  it("offers the same words as a tooltip", () => {
    openMenu();
    expect(screen.getAllByRole("radio").map((r) => r.getAttribute("title"))).toEqual(["Light", "System", "Dark"]);
  });

  it("hides the glyph itself from assistive tech, so the name is not read twice", () => {
    openMenu();
    for (const r of screen.getAllByRole("radio")) {
      expect(r.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("marks the current setting, and moves the mark when another is picked", () => {
    openMenu();
    const checked = () =>
      screen.getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true");

    expect(checked()).toHaveAttribute("aria-label", "System");
    act(() => screen.getByRole("radio", { name: "Dark" }).click());
    expect(checked()).toHaveAttribute("aria-label", "Dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  // The whole .header-user toggles the menu, so a click that bubbled would shut
  // it — and comparing the three settings means reopening it every time.
  it("leaves the menu open so the settings can be compared", () => {
    openMenu();
    act(() => screen.getByRole("radio", { name: "Light" }).click());
    expect(screen.getAllByRole("radio")).toHaveLength(3);
  });
});

/**
 * The keyboard contract that comes with role="radiogroup". Choosing the radio
 * role over three independent toggle buttons is what obliges this: the set is
 * announced as one control with three options, and a keyboard user expects one
 * tab stop with the arrows moving inside it.
 */
describe("the appearance switch by keyboard", () => {
  const group = () => screen.getByRole("radiogroup");
  const labels = () => screen.getAllByRole("radio").map((r) => r.getAttribute("aria-label"));
  const focused = () => document.activeElement?.getAttribute("aria-label");
  const checked = () =>
    screen
      .getAllByRole("radio")
      .find((r) => r.getAttribute("aria-checked") === "true")
      ?.getAttribute("aria-label");

  it("is one tab stop, on whichever option is checked", () => {
    openMenu();
    const tabbable = screen.getAllByRole("radio").filter((r) => r.getAttribute("tabindex") === "0");
    expect(tabbable).toHaveLength(1);
    expect(tabbable[0]).toHaveAttribute("aria-label", checked()!);
  });

  it("moves the selection right, and takes focus with it", () => {
    openMenu();
    expect(labels()).toEqual(["Light", "System", "Dark"]);
    expect(checked()).toBe("System");

    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" }));
    expect(checked()).toBe("Dark");
    expect(focused()).toBe("Dark");
  });

  it("moves the selection left", () => {
    openMenu();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" }));
    expect(checked()).toBe("Light");
    expect(focused()).toBe("Light");
  });

  it("wraps at both ends rather than stopping", () => {
    openMenu();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" })); // to Light
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" })); // wraps to Dark
    expect(checked()).toBe("Dark");

    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" })); // wraps to Light
    expect(checked()).toBe("Light");
  });

  it("takes Home and End to the ends", () => {
    openMenu();
    act(() => void fireEvent.keyDown(group(), { key: "End" }));
    expect(checked()).toBe("Dark");
    act(() => void fireEvent.keyDown(group(), { key: "Home" }));
    expect(checked()).toBe("Light");
  });

  // Without preventDefault the arrows scroll the dropdown and Home/End jump the
  // page, while the selection moves underneath. Everything else must pass.
  it("swallows only the keys it handles", () => {
    openMenu();
    expect(fireEvent.keyDown(group(), { key: "ArrowRight" })).toBe(false);
    expect(fireEvent.keyDown(group(), { key: "a" })).toBe(true);
  });

  it("applies an arrow selection to the document, not just the markup", () => {
    openMenu();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
