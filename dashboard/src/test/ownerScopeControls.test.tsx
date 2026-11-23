import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CLAIM_PATH, ClaimPathNote } from "../components/ClaimPath";
import { ScopeSwitch } from "../components/ScopeSwitch";
import { YoursBadge } from "../components/YoursBadge";
import css from "../components/ownerScope.css?raw";
import { declarations, TOKENS } from "./paletteTokens";

describe("ScopeSwitch", () => {
  it("is a group named for what it chooses, of two native radios named by their text", () => {
    render(<ScopeSwitch value="mine" onChange={() => {}} />);
    const group = screen.getByRole("group", { name: "Show data from" });
    const radios = within(group).getAllByRole("radio");

    expect(radios).toHaveLength(2);
    expect(within(group).getByRole("radio", { name: "My nodes" })).toBe(radios[0]);
    expect(within(group).getByRole("radio", { name: "Whole network" })).toBe(radios[1]);
    for (const radio of radios) {
      expect(radio.tagName).toBe("INPUT");
      expect(radio).toHaveAttribute("type", "radio");
    }
  });

  // What the browser needs to give the group one tab stop at the checked
  // option and move between the two with the arrow keys: one shared name, and
  // no tabindex of ours overriding it.
  it("leaves keyboard order to the browser: one group name, no tabindex", () => {
    render(<ScopeSwitch value="network" onChange={() => {}} />);
    const [mine, network] = screen.getAllByRole("radio") as HTMLInputElement[];

    expect(mine.name).not.toBe("");
    expect(network.name).toBe(mine.name);
    expect(mine).not.toHaveAttribute("tabindex");
    expect(network).not.toHaveAttribute("tabindex");

    network.focus();
    expect(network).toHaveFocus();
  });

  it("gives each switch on a page its own group", () => {
    render(
      <>
        <ScopeSwitch value="mine" onChange={() => {}} label="Detections from" />
        <ScopeSwitch value="network" onChange={() => {}} label="Anomalies from" />
      </>,
    );
    const first = within(screen.getByRole("group", { name: "Detections from" })).getAllByRole("radio");
    const second = within(screen.getByRole("group", { name: "Anomalies from" })).getAllByRole("radio");

    expect((first[0] as HTMLInputElement).name).not.toBe((second[0] as HTMLInputElement).name);
    expect(first[0]).toBeChecked();
    expect(second[1]).toBeChecked();
  });

  it("checks the option its value names", () => {
    const { rerender } = render(<ScopeSwitch value="mine" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "My nodes" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Whole network" })).not.toBeChecked();

    rerender(<ScopeSwitch value="network" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "My nodes" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
  });

  it("reports a choice of the other option, and leaves the value to the page", () => {
    const onChange = vi.fn();
    render(<ScopeSwitch value="mine" onChange={onChange} />);

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("network");
    // Controlled: nothing moves until the page passes the new value.
    expect(screen.getByRole("radio", { name: "My nodes" })).toBeChecked();

    // Choosing the option already chosen is no change.
    fireEvent.click(screen.getByRole("radio", { name: "My nodes" }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("takes a click on the segment's text as a choice", () => {
    const onChange = vi.fn();
    render(<ScopeSwitch value="network" onChange={onChange} />);

    fireEvent.click(screen.getByText("My nodes"));
    expect(onChange).toHaveBeenCalledWith("mine");
  });

  it("takes its labels from the page", () => {
    render(
      <ScopeSwitch value="mine" onChange={() => {}} label="Anomalies from" mineLabel="Mine" networkLabel="Everyone's" />,
    );
    const group = screen.getByRole("group", { name: "Anomalies from" });
    expect(within(group).getByRole("radio", { name: "Mine" })).toBeChecked();
    expect(within(group).getByRole("radio", { name: "Everyone's" })).not.toBeChecked();
  });

  it("shows how many nodes are the owner's, and reads it without the dot", () => {
    render(<ScopeSwitch value="mine" onChange={() => {}} mineCount={3} />);
    const mine = screen.getByRole("radio", { name: "My nodes 3" });

    expect(mine.closest("label")).toHaveTextContent("My nodes · 3");
    expect(screen.getByRole("radio", { name: "Whole network" }).closest("label")).toHaveTextContent(/^Whole network$/);
  });

  it("shows a count of none, which is a count", () => {
    render(<ScopeSwitch value="mine" onChange={() => {}} mineCount={0} />);
    expect(screen.getByRole("radio", { name: "My nodes 0" }).closest("label")).toHaveTextContent("My nodes · 0");
  });

  it("shows no count when it is not given", () => {
    render(<ScopeSwitch value="mine" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: "My nodes" }).closest("label")).toHaveTextContent(/^My nodes$/);
  });

  it("disables both options, and the group, when disabled", () => {
    render(<ScopeSwitch value="mine" onChange={() => {}} disabled />);
    expect(screen.getByRole("group", { name: "Show data from" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "My nodes" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Whole network" })).toBeDisabled();
  });

  it("is enabled by default", () => {
    render(<ScopeSwitch value="mine" onChange={() => {}} />);
    for (const radio of screen.getAllByRole("radio")) expect(radio).toBeEnabled();
  });
});

describe("ClaimPathNote", () => {
  it("names the claim path", () => {
    const { container } = render(<ClaimPathNote />);
    expect(container.firstElementChild).toHaveTextContent(CLAIM_PATH, { normalizeWhitespace: false });
    expect(container.textContent).toBe(CLAIM_PATH);
  });

  it("puts the page's own sentence first", () => {
    const { container } = render(<ClaimPathNote lead="You have no nodes yet." />);
    expect(container.textContent).toBe(`You have no nodes yet. ${CLAIM_PATH}`);
  });

  it("takes markup as its lead", () => {
    const { container } = render(
      <ClaimPathNote
        lead={
          <>
            Nothing from <strong>your nodes</strong> in this window.
          </>
        }
      />,
    );
    expect(screen.getByText("your nodes").tagName).toBe("STRONG");
    expect(container.textContent).toBe(`Nothing from your nodes in this window. ${CLAIM_PATH}`);
  });

  // An alert is announced the moment it renders; this is a note a reader comes
  // to, rendered on every visit to an owner view.
  it("is information, not an alert", () => {
    const { container } = render(<ClaimPathNote lead="You have no nodes yet." />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(container.querySelector("[aria-live]")).toBeNull();
  });

  it("hides its glyph from assistive tech", () => {
    const { container } = render(<ClaimPathNote />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("YoursBadge", () => {
  it("is the leaderboard's tag", () => {
    const { container } = render(<YoursBadge />);
    expect(container.innerHTML).toBe('<span class="badge plain yours">Yours</span>');
  });
});

/* The switch and the note are drawn from tokens alone; these read which ones
   the stylesheet spends and check each pairing stays readable in both themes. */
describe("ownerScope.css", () => {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (selector: string) => declarations(bare, `${selector} {`);

  type Rgb = [number, number, number];

  function parse(value: string): { rgb: Rgb; alpha: number } {
    const hex = /^#([0-9a-f]{6})$/i.exec(value);
    if (hex) {
      const n = parseInt(hex[1], 16);
      return { rgb: [(n >> 16) & 255, (n >> 8) & 255, n & 255], alpha: 1 };
    }
    const rgba = /^rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)$/i.exec(value);
    if (rgba) return { rgb: [+rgba[1], +rgba[2], +rgba[3]], alpha: +rgba[4] };
    throw new Error(`cannot read the colour ${value}`);
  }

  /** What a background paints, its layers laid bottom-up on `under`. Each
   *  token once: a gradient spells its colour twice to make a flat layer. */
  function paint(theme: keyof typeof TOKENS, background: string, under?: Rgb): Rgb {
    const layers = [...new Set([...background.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]))].reverse();
    expect(layers.length, `no token in ${background}`).toBeGreaterThan(0);
    let rgb = under;
    for (const name of layers) {
      const { rgb: top, alpha } = parse(TOKENS[theme](name));
      if (alpha < 1 && !rgb) throw new Error(`${name} is translucent and has nothing under it`);
      rgb = alpha === 1 ? top : (top.map((c, i) => c * alpha + rgb![i] * (1 - alpha)) as Rgb);
    }
    return rgb!;
  }

  function luminance([r, g, b]: Rgb): number {
    const lin = (c: number) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  }

  function contrast(theme: keyof typeof TOKENS, ink: string, ground: Rgb): number {
    const [a, b] = [luminance(paint(theme, ink)), luminance(ground)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  }

  const THEMES = ["light", "dark"] as const;

  it.each(THEMES)("keeps the chosen segment's text at 4.5:1 or better in the %s theme", (theme) => {
    const checked = rule(".scope-switch-option input:checked + .scope-switch-text");
    expect(contrast(theme, checked.color, paint(theme, checked.background))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(THEMES)("keeps the other segment's text at 4.5:1 or better in the %s theme, at rest and hovered", (theme) => {
    const track = paint(theme, rule(".scope-switch-track").background);
    expect(contrast(theme, rule(".scope-switch-text").color, track)).toBeGreaterThanOrEqual(4.5);

    const hover = rule(".scope-switch-option:hover input:not(:checked):not(:disabled) + .scope-switch-text");
    expect(contrast(theme, hover.color, paint(theme, hover.background, track))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(THEMES)("keeps the note readable on the page and in a card in the %s theme", (theme) => {
    const note = rule(".claim-path");
    for (const ground of ["var(--bg-primary)", "var(--bg-card)"]) {
      const wash = paint(theme, note.background, paint(theme, ground));
      expect(contrast(theme, note.color, wash)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(theme, rule(".claim-path-lead").color, wash)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("rings the focused segment", () => {
    expect(rule(".scope-switch-option input:focus-visible + .scope-switch-text").outline).toBe("2px solid var(--accent)");
    // After the checked rule, which is as specific, so a focused choice shows it.
    expect(bare.indexOf("input:focus-visible")).toBeGreaterThan(bare.indexOf("input:checked + .scope-switch-text"));
  });

  it("gives each segment a 32px target", () => {
    const text = rule(".scope-switch-text");
    const [vertical] = text.padding.split(" ");
    expect(parseFloat(text["line-height"]) + 2 * parseFloat(vertical)).toBeGreaterThanOrEqual(32);
  });

  it("lets the switch take a phone's row, and wrap rather than overflow", () => {
    const narrow = bare.slice(bare.indexOf("@media (max-width: 480px)"));
    expect(narrow.length).toBeLessThan(bare.length);
    expect(declarations(narrow, ".scope-switch {").width).toBe("100%");
    expect(declarations(narrow, ".scope-switch-text {")["white-space"]).toBe("normal");
    expect(rule(".scope-switch")["max-width"]).toBe("100%");
    expect(rule(".scope-switch")["min-width"]).toBe("0");
  });
});
