import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DayEntry } from "../../pages/user/dataExplorer/archive";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { ResultsTree } from "../../pages/user/dataExplorer/ResultsTree";
import phoneCss from "../../pages/user/dataExplorer/resultsTree.phone.css?raw";
import { stubViewport } from "../matchMedia";

/* On a phone the results tree lists each file as a card, with its actions in
   a menu, and sorts from a select. Selection works as on the desktop, and a
   width past the phone's gets the desktop's rows back. */

afterEach(() => vi.unstubAllGlobals());

const DAY = "2026-09-17";
/** A flushed file's name: fifty characters, most of them one hex id. */
const LONG = "part-00000-3f2a9c1e4b7d4e0f9a8b7c6d5e4f3a2b.parquet";

const file = (node: string, name: string, over: Partial<ArchiveFile> = {}): ArchiveFile => ({
  key: `${DAY}/${node}/${name}`,
  name,
  node,
  day: DAY,
  size: 1024,
  endMs: Date.parse(`${DAY}T06:00:00Z`),
  startMs: Date.parse(`${DAY}T05:00:00Z`),
  firstFrameMs: null,
  publishedLat: null,
  publishedLon: null,
  ...over,
});

const keys = (files: ArchiveFile[]) => files.map((f) => f.key);

/** A file the archive map can open: a first frame and a published position. */
const mappable = { firstFrameMs: Date.UTC(2026, 8, 17, 5, 10, 0, 500), publishedLat: 51.62, publishedLon: -0.52 };

type Props = Parameters<typeof ResultsTree>[0];

function props(files: ArchiveFile[], over: Partial<Props> = {}): Props {
  return {
    days: [DAY],
    byDay: new Map([[DAY, files]]),
    entryFor: (day): DayEntry => ({ day, nodeRef: null, status: "done", files: [] }),
    sort: "span",
    onSort: vi.fn(),
    onRetry: vi.fn(),
    selected: new Set(),
    onSelect: vi.fn(),
    active: null,
    onPreview: vi.fn(),
    ...over,
  };
}

/** The tree on a phone 390px wide. */
function setup(files: ArchiveFile[], over: Partial<Props> = {}) {
  const viewport = stubViewport(390);
  const p = props(files, over);
  const view = render(<ResultsTree {...p} />);
  return { ...p, view, viewport };
}

/** Opens a file's actions menu, as a tap on its button would. */
function openMenu(name: string) {
  const button = screen.getByRole("button", { name: `Actions for ${name}` });
  fireEvent.click(button);
  return { button, sheet: within(screen.getByRole("dialog", { name })) };
}

describe("the results tree on a phone", () => {
  it("lists a file as a card that keeps its name's end in sight and the whole name in its title", () => {
    const { view } = setup([file("ret-a", LONG)], { active: `${DAY}/ret-a/${LONG}` });
    const card = screen.getByTestId("de-file");
    expect(card).toHaveClass("de-filecard", "active");
    expect(view.container.querySelector(".de-file")).toBeNull();

    const name = card.querySelector(".de-fname");
    expect(name).toHaveAttribute("title", LONG);
    expect(name).toHaveTextContent(LONG);
    expect(card.querySelector(".de-fname-tail")).toHaveTextContent(/^5e4f3a2b\.parquet$/);
    expect(within(card).getByText("05:00 → 06:00")).toBeInTheDocument();
    expect(within(card).getByText("1.0 KB")).toBeInTheDocument();
  });

  it("opens a file's actions in a sheet named for it, whose Preview closes it and gives focus back to its button", () => {
    const { onPreview } = setup([file("ret-a", "a.parquet")]);
    const { button, sheet } = openMenu("a.parquet");
    expect(button).toHaveAttribute("aria-haspopup", "dialog");
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("dialog")).toHaveAccessibleDescription("05:00 → 06:00 est. · 1.0 KB");

    fireEvent.click(sheet.getByRole("button", { name: "Preview" }));
    expect(onPreview).toHaveBeenCalledTimes(1);
    expect(onPreview).toHaveBeenCalledWith(`${DAY}/ret-a/a.parquet`);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button).toHaveFocus();
    expect(button).toHaveAttribute("aria-expanded", "false");

    // Escape closes it the same way.
    button.blur();
    openMenu("a.parquet");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button).toHaveFocus();
  });

  it("downloads a file's JSON from its menu by the desktop's link", () => {
    setup([file("ret-a", "a.parquet")], { source: "owner" });
    const { sheet } = openMenu("a.parquet");
    const link = sheet.getByRole("link", { name: /^Download JSON/ });
    expect(link).toHaveAttribute("href", `/api/auth/me/archive/${DAY}/ret-a/a.parquet`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveTextContent("≈ 9.0 KB");
  });

  it("adds a file to the basket from its menu, and takes it out again once it is in", () => {
    const key = `${DAY}/ret-a/a.parquet`;
    stubViewport(390);
    const p = props([file("ret-a", "a.parquet")]);
    const { rerender } = render(<ResultsTree {...p} />);
    fireEvent.click(openMenu("a.parquet").sheet.getByRole("button", { name: "Add to basket" }));
    expect(p.onSelect).toHaveBeenLastCalledWith([key], true);
    expect(screen.queryByRole("dialog")).toBeNull();

    rerender(<ResultsTree {...p} selected={new Set([key])} />);
    const { sheet } = openMenu("a.parquet");
    expect(sheet.queryByRole("button", { name: "Add to basket" })).toBeNull();
    fireEvent.click(sheet.getByRole("button", { name: "Remove from basket" }));
    expect(p.onSelect).toHaveBeenLastCalledWith([key], false);
  });

  it("says in words why a private node's file cannot go on the map, and links a public one there", () => {
    setup([file("ret-a", "mine.parquet", mappable), file("ret-b", "public.parquet", mappable)], {
      scope: "mine",
      privateRefs: new Set(["ret-a"]),
    });
    const reason = "Private node: the public map leaves it out.";

    let { sheet } = openMenu("mine.parquet");
    const off = sheet.getByRole("button", { name: "Show on map" });
    expect(off).toBeDisabled();
    expect(off).toHaveAccessibleDescription(reason);
    expect(off).not.toHaveAttribute("title");
    expect(sheet.getByText(reason)).toBeVisible();
    expect(sheet.queryByRole("link", { name: "Show on map" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    ({ sheet } = openMenu("public.parquet"));
    const link = sheet.getByRole("link", { name: "Show on map" });
    expect(link).toHaveAttribute("href", "/map#lat=51.6200&lon=-0.5200&z=9&t=2026-09-17T05:10:00Z");
    expect(link).toHaveAttribute("target", "_blank");
    expect(sheet.queryByText(reason)).toBeNull();
  });

  it("opens the preview from a tap on the card, but not from its checkbox or its menu", () => {
    const { onPreview, onSelect } = setup([file("ret-a", "a.parquet")]);
    const card = screen.getByTestId("de-file");
    const box = within(card).getByRole("checkbox", { name: "Select a.parquet" });

    fireEvent.click(box);
    // The room round the glyph is the checkbox's target too.
    const target = box.closest("label");
    expect(target).toHaveClass("de-check");
    fireEvent.click(target!);
    expect(onSelect).toHaveBeenCalledTimes(2);
    fireEvent.click(within(card).getByRole("button", { name: "Actions for a.parquet" }));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onPreview).not.toHaveBeenCalled();

    fireEvent.click(card.querySelector(".de-fname")!);
    expect(onPreview).toHaveBeenCalledWith(`${DAY}/ret-a/a.parquet`);
  });

  it("gives focus to the card's actions button when the card is tapped, so closing the preview returns there", () => {
    const { onPreview } = setup([file("ret-a", "a.parquet"), file("ret-a", "b.parquet")]);
    const [card] = screen.getAllByTestId("de-file");

    fireEvent.click(card.querySelector(".de-fname")!);
    expect(onPreview).toHaveBeenCalledWith(`${DAY}/ret-a/a.parquet`);
    expect(document.activeElement).toBe(within(card).getByRole("button", { name: "Actions for a.parquet" }));
  });

  it("ticks a day, a node and everything matched as on a desktop, without toggling a group", () => {
    const files = [file("ret-a", "a1.parquet"), file("ret-a", "a2.parquet"), file("ret-b", "b1.parquet")];
    const { onSelect } = setup(files, {
      days: ["2026-09-16", DAY],
      byDay: new Map([[DAY, files]]),
    });
    // Each head's checkbox in a fingertip's target, which a tap ticks.
    const node = screen.getByRole("checkbox", { name: `Select every file from ret-a on ${DAY}` });
    expect(node.closest("label")).toHaveClass("de-check");
    fireEvent.click(node.closest("label")!);
    expect(onSelect).toHaveBeenLastCalledWith([files[0].key, files[1].key], true);

    const day = screen.getByRole("checkbox", { name: `Select every file on ${DAY}` });
    fireEvent.click(day.closest("label")!);
    expect(onSelect).toHaveBeenLastCalledWith(keys(files), true);
    expect(screen.getByRole("button", { name: new RegExp(`^${DAY}`) })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /^ret-a/ })).toHaveAttribute("aria-expanded", "true");

    // A day with nothing matched has nothing to tick, and its target does
    // not toggle it either.
    const empty = screen.getByRole("checkbox", { name: "Select every file on 2026-09-16" });
    expect(empty).toBeDisabled();
    fireEvent.click(empty.closest("label")!);
    expect(screen.getByRole("button", { name: /^2026-09-16/ })).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(screen.getByRole("button", { name: "Select all matching" }));
    expect(onSelect).toHaveBeenLastCalledWith(keys(files), true);
    expect(onSelect).toHaveBeenCalledTimes(3);
  });

  it("sorts from a select in the column headers' place", () => {
    const { onSort, view } = setup([file("ret-a", "a.parquet")]);
    expect(view.container.querySelector(".de-colhead")).toBeNull();
    const select = screen.getByLabelText("Sort by");
    expect(select).toHaveClass("de-sort");
    expect(select).toHaveValue("span");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["File", "Coverage", "Size"]);
    fireEvent.change(select, { target: { value: "size" } });
    expect(onSort).toHaveBeenCalledWith("size");
  });

  it("gives the desktop's rows back past the phone width, and leaves no menu open behind", () => {
    const { view, viewport } = setup([file("ret-a", "a.parquet")]);
    const { container } = view;
    expect(container.querySelector(".de-filecard")).not.toBeNull();
    openMenu("a.parquet");

    viewport.resize(1440);
    expect(container.querySelector(".de-file")).not.toBeNull();
    expect(container.querySelector(".de-colhead")).not.toBeNull();
    for (const phoneOnly of [".de-filecard", ".de-sort", ".de-check", ".de-file-menu"]) {
      expect(container.querySelector(phoneOnly), phoneOnly).toBeNull();
    }
    expect(screen.queryByRole("dialog")).toBeNull();

    viewport.resize(390);
    expect(container.querySelector(".de-filecard")).not.toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});


/** The prelude of each top-level block, comments left out. */
function topLevel(css: string): string[] {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const preludes: string[] = [];
  for (let i = 0, depth = 0, start = 0; i < bare.length; i++) {
    if (bare[i] === "{" && depth++ === 0) preludes.push(bare.slice(start, i).trim());
    else if (bare[i] === "}" && --depth === 0) start = i + 1;
  }
  return preludes;
}

describe("the phone stylesheet", () => {
  // A desktop at 1024px and up must be drawn by dataExplorer.css alone.
  it("puts every rule inside a max-width block below desktop width", () => {
    const preludes = topLevel(phoneCss);
    expect(preludes.length).toBeGreaterThan(0);
    const below = (prelude: string) =>
      /^@media /.test(prelude) &&
      prelude
        .slice("@media ".length)
        .split(",")
        .every((query) => {
          const max = /\(max-width:\s*([\d.]+)px\)/.exec(query);
          return max !== null && Number(max[1]) < 1024 && !/min-width/.test(query);
        });
    expect(preludes.filter((prelude) => !below(prelude))).toEqual([]);
  });

  it("highlights a file action under the pointer only where the pointer can hover", () => {
    const bare = phoneCss.replace(/\/\*[\s\S]*?\*\//g, "");
    const hovers = [...bare.matchAll(/\.de-file-action:hover/g)].map((m) => m.index);
    expect(hovers).toHaveLength(1);
    let prelude = "";
    for (let i = 0, depth = 0, start = 0; i < hovers[0]; i++) {
      if (bare[i] === "{" && depth++ === 0) prelude = bare.slice(start, i).trim();
      else if (bare[i] === "}" && --depth === 0) start = i + 1;
    }
    expect(prelude).toContain("(hover: hover)");
  });

  it("keeps the end of a long name in sight while its head gives way", () => {
    const rule = (selector: string) => {
      const at = phoneCss.indexOf(`${selector} {`);
      expect(at, selector).toBeGreaterThan(-1);
      return phoneCss.slice(at, phoneCss.indexOf("}", at));
    };
    expect(rule(".de-fname-head")).toMatch(/text-overflow:\s*ellipsis/);
    expect(rule(".de-fname-tail")).toMatch(/flex:\s*none/);
  });
});
