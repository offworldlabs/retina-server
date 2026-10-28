import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";
import explorerCss from "../../pages/user/dataExplorer/dataExplorer.css?raw";
import { stubViewport } from "../matchMedia";

/* On a phone the filters card folds behind one line that says what the
   filters are, so the results are not a screen and a half below the stats,
   and opens into rows that fit a 360px screen. A tablet and a desktop keep
   the card as it was. */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));
// A visitor, so the page reads the network's archive and asks for no owner list.
vi.mock("../../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

const FLEET = { "ret-a": { name: "A" }, "ret-b": { name: "B" }, "ret-c": { name: "C" } };

/** One file a day, on ret-a, for whichever day a listing asks about. */
function route(url: string): Promise<unknown> {
  if (!url.startsWith("/api/data/archive")) return Promise.resolve({ nodes: FLEET });
  const day = (new URL(url, "http://localhost").searchParams.get("date") ?? "").replace(/\//g, "-");
  const key = `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_ref=ret-a/part-0.parquet`;
  return Promise.resolve({ files: [{ key, size_bytes: 2 * 1024 * 1024, modified: `${day}T06:00:00Z` }], total: 1 });
}

// Every filter set, the optional ones included.
const LINK =
  "?node=ret-a&node=ret-b&from=2026-09-11&to=2026-09-17&tod=06:00-18:30&minsize=1048576&near=51.5074,-0.1278,25";
const SUMMARY =
  "2 of 3 nodes · 2026-09-11 → 2026-09-17 · 06:00–18:30 UTC · ≥ 1 MB · within 25 km of 51.5074, -0.1278";

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-17T12:00:00Z"));
  requestMock.mockReset();
  requestMock.mockImplementation(route);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the filters card on a phone", () => {
  it("folds behind a line that sums the filters up, opens and folds at a tap, and stays open on a desktop", async () => {
    const viewport = stubViewport(390);
    const { container } = render(
      <MemoryRouter initialEntries={[`/data${LINK}`]}>
        <DataExplorerPage />
      </MemoryRouter>,
    );
    const summary = () => container.querySelector(".de-fsummary");
    const from = () => container.querySelector("#de-from");
    const map = () => container.querySelector(".de-map");
    const copy = () => screen.queryByRole("button", { name: "Copy link" });

    // Folded on arrival, its caret left out of what the line is called.
    await waitFor(() => expect(summary()).toHaveAccessibleName(SUMMARY));
    expect(summary()).toHaveAttribute("aria-expanded", "false");
    expect(from()).not.toBeVisible();
    expect(copy()).toBeNull();
    expect(screen.getByRole("button", { name: "Reset" })).toBeVisible();

    fireEvent.click(summary()!);
    expect(summary()).toHaveAttribute("aria-expanded", "true");
    expect(from()).toBeVisible();
    expect(copy()).toBeVisible();

    // The map, once open, folds away with the fields.
    fireEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(map()).toBeVisible();

    fireEvent.click(summary()!);
    expect(summary()).toHaveAttribute("aria-expanded", "false");
    expect(from()).not.toBeVisible();
    expect(map()).not.toBeVisible();
    expect(copy()).toBeNull();
    expect(screen.getByRole("button", { name: "Reset" })).toBeVisible();

    viewport.resize(1440);
    expect(summary()).toBeNull();
    expect(from()).toBeVisible();
    expect(copy()).toBeVisible();
    expect(map()).toBeVisible();
  });
});

/** The stylesheet's rules, each with the @media block it sits in, if any. */
interface Rule {
  media: string | null;
  selector: string;
  decls: Map<string, string>;
}

/** Each top-level `prelude { body }` in `text`, braces balanced. */
function blocks(text: string): [string, string][] {
  const out: [string, string][] = [];
  let at = 0;
  for (let open = text.indexOf("{", at); open >= 0; open = text.indexOf("{", at)) {
    let close = open + 1;
    for (let depth = 1; depth > 0; close++) depth += text[close] === "{" ? 1 : text[close] === "}" ? -1 : 0;
    out.push([text.slice(at, open).trim(), text.slice(open + 1, close - 1)]);
    at = close;
  }
  return out;
}

function declarations(body: string): Map<string, string> {
  const pairs = body.split(";").map((d) => d.split(":").map((s) => s.trim()));
  return new Map(pairs.filter(([prop, value]) => prop && value).map(([prop, ...value]) => [prop, value.join(":")]));
}

const RULES: Rule[] = blocks(explorerCss.replace(/\/\*[\s\S]*?\*\//g, "")).flatMap(([prelude, body]) =>
  prelude.startsWith("@media")
    ? blocks(body).map(([selector, decls]) => ({ media: prelude, selector, decls: declarations(decls) }))
    : [{ media: null, selector: prelude, decls: declarations(body) }],
);

const PHONE_BLOCK = "@media (max-width: 640px)";
// phone.css's query for finger-sized targets: every phone, and a tablet driven by touch.
const TOUCH_BLOCK = "@media (max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";
// The availability strip's touch rules, for any screen narrower than a desktop.
const STRIP_BLOCK = "@media (max-width: 1023.98px)";

describe("the filters card's narrow styles", () => {
  // A desktop at 1024px and up must draw the card as it always has.
  it("sit in blocks below desktop width, and the phone's own parts in the phone's block", () => {
    const narrow = new Set(
      RULES.map((r) => r.media).filter(
        (media): media is string =>
          media !== null && [...media.matchAll(/max-width:\s*([\d.]+)px/g)].some(([, px]) => Number(px) < 1024),
      ),
    );
    expect([...narrow].sort()).toEqual([TOUCH_BLOCK, STRIP_BLOCK, PHONE_BLOCK].sort());

    const phoneOnly = RULES.filter((r) => /\.de-fsummary|\.de-near-row/.test(r.selector));
    expect(phoneOnly.length).toBeGreaterThan(0);
    expect(phoneOnly.filter((r) => r.media !== PHONE_BLOCK).map((r) => r.selector)).toEqual([]);
  });

  it("lay the centre out on a phone as a two-column grid of fields with no fixed width", () => {
    const phone = RULES.filter((r) => r.media === PHONE_BLOCK);
    const row = phone.find((r) => r.selector === ".de-near-row");
    expect(row?.decls.get("display")).toBe("grid");
    const tracks = row?.decls.get("grid-template-columns")?.match(/\w+\([^)]*\)|[^\s()]+/g) ?? [];
    expect(tracks).toHaveLength(2);
    expect(tracks.filter((t) => /\d+px/.test(t))).toEqual([]);

    // The wide card's fixed widths (96px coordinates, a 70px radius, a 198px
    // chip) are each taken back inside the row, and none set in their place.
    const widths = phone
      .filter((r) => r.selector.startsWith(".de-near-row "))
      .flatMap((r) => [...r.decls].filter(([prop]) => /(^|-)width$/.test(prop)).map(([p, v]) => `${r.selector} ${p}: ${v}`));
    expect(widths.filter((w) => /\d+px/.test(w))).toEqual([]);
    expect(phone.find((r) => /^\.de-near-row > input\b/.test(r.selector))?.decls.get("width")).toBe("100%");
    expect(phone.find((r) => r.selector === ".de-near-row > .de-near-node")?.decls.get("width")).toBe("auto");
  });

  // A column that wraps stretches each filter to its line, which is as wide as
  // the widest filter; a single line stretches each to the card.
  it("stack the filters on a phone in a single line, so each is as wide as the card", () => {
    const filters = RULES.find((r) => r.media === PHONE_BLOCK && r.selector === ".de-filters");
    expect(filters?.decls.get("flex-direction")).toBe("column");
    expect(filters?.decls.get("flex-wrap")).toBe("nowrap");
    expect(filters?.decls.get("align-items")).toBe("stretch");
  });

  // A 360px screen leaves the card's body 270px across, and the arrow takes
  // 12px of it. Chromium draws a date with 22px of borders and picker icon
  // beside up to 94px of digits, "04/04/2004" at 16px; a time needs less.
  it("leave each date and time a half on a 360px screen that holds its whole value", () => {
    const phone = RULES.filter((r) => r.media === PHONE_BLOCK);
    const rule = (selector: string) => phone.find((r) => r.selector.split(",").some((s) => s.trim() === selector));
    const px = (r: Rule | undefined, prop: string) => Number(/^([\d.]+)px$/.exec(r?.decls.get(prop) ?? "")?.[1] ?? NaN);
    for (const type of ["date", "time"]) {
      const row = rule(`.de-frow:has(> input[type="${type}"])`);
      const field = rule(`.de-frow > input[type="${type}"]`);
      expect(row, `the row of ${type}s`).toBeDefined();
      const half = (270 - 12 - 2 * px(row, "gap")) / 2;
      const needed = 22 + px(field, "padding-left") + px(field, "padding-right") + 94;
      expect(needed, `a ${type} in a ${half}px half`).toBeLessThanOrEqual(half);
    }
  });
});
