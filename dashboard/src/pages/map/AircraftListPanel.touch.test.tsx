import { beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import css from "./LiveAircraftMap.css?raw";
import phoneCss from "../../styles/phone.css?raw";
import AircraftListPanel from "./AircraftListPanel";
import { MapThemeProvider } from "./useMapTheme";
import { stubViewport } from "../../test/matchMedia";

// On a phone or a touch tablet the rows are 44px apart from the list's top
// edge, as a mouse's 40px rows are, and each row's pin star sits halfway down
// its row, where the 44px box centred on it fills the row's height.

// jsdom has no ResizeObserver, and the list sizes its virtual window with one.
beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

const FLEET = Array.from({ length: 30 }, (_, i) => ({
  hex: `a0${String(i).padStart(4, "0")}`,
  flight: `ROW${String(i).padStart(2, "0")}`,
  // Descending, so the default altitude sort keeps them in this order.
  alt_baro: 40000 - i * 100,
}));

function renderList({ selectedHex = null, pinnable = true }: { selectedHex?: string | null; pinnable?: boolean } = {}) {
  const { container } = render(
    <MapThemeProvider>
      <AircraftListPanel
        allAircraft={FLEET}
        truthOnly={[]}
        selectedHex={selectedHex}
        onSelect={() => {}}
        collapsed={false}
        onToggleCollapse={() => {}}
        searchQuery=""
        onSearchChange={() => {}}
        searchInputRef={{ current: null }}
        onSortChange={() => {}}
        pinned={new Set()}
        onTogglePin={pinnable ? () => {} : undefined}
        userLoc={null}
        header={null}
      />
    </MapThemeProvider>,
  );
  // The scroller, the box that gives it its full height, and the band that
  // holds the rows drawn, moved down to the first of them.
  const list = container.querySelector<HTMLElement>(".al-list")!;
  const track = list.lastElementChild as HTMLElement;
  const band = track.firstElementChild as HTMLElement;
  const firstDrawn = () => band.querySelector(".al-callsign")?.textContent?.replace("★", "");
  return { container, list, track, band, firstDrawn };
}

function scrollTo(list: HTMLElement, top: number) {
  list.scrollTop = top;
  fireEvent.scroll(list);
}

describe("the aircraft list's rows on a touch screen", () => {
  it.each([
    ["a phone", 390, false],
    ["a touch tablet", 768, true],
  ])("are 44px apart on %s, from the list's top", (_, width, coarse) => {
    stubViewport(width, { coarse });
    const { list, track, band, firstDrawn } = renderList();
    expect(track.style.height).toBe("1320px");
    expect(band.style.top).toBe("0px");
    expect(firstDrawn()).toBe("ROW00");

    // Rows 0 to 9 have gone past the top (10 × 44 = 440), and five are kept
    // above row 10.
    scrollTo(list, 440);
    expect(band.style.top).toBe("220px");
    expect(firstDrawn()).toBe("ROW05");
  });

  it("scrolls a selected row into view at that spacing", () => {
    stubViewport(768, { coarse: true });
    const { list } = renderList({ selectedHex: FLEET[20].hex });
    // Row 20's middle, at 20 × 44 + 22, in the middle of a list jsdom gives
    // no height.
    expect(list.scrollTop).toBe(902);
  });
});

describe("the aircraft list's rows with a mouse", () => {
  it.each([
    ["a tablet", 768],
    ["a desktop", 1280],
  ])("are 40px apart on %s, from the list's top", (_, width) => {
    stubViewport(width);
    const { list, track, band, firstDrawn } = renderList();
    expect(track.style.height).toBe("1200px");
    expect(band.style.top).toBe("0px");
    expect(firstDrawn()).toBe("ROW00");

    scrollTo(list, 440);
    expect(band.style.top).toBe("240px");
    expect(firstDrawn()).toBe("ROW06");
  });

  it("scrolls a selected row into view at that spacing", () => {
    stubViewport(1280);
    const { list } = renderList({ selectedHex: FLEET[20].hex });
    expect(list.scrollTop).toBe(820);
  });
});

describe("the aircraft list's pin stars", () => {
  it.each([
    ["a touch tablet", 768, true],
    ["a desktop", 1280, false],
  ])("carry the class the touch rules size them by on %s", (_, width, coarse) => {
    stubViewport(width, { coarse });
    const stars = renderList().container.querySelectorAll('button[title="Pin to top"]');
    expect(stars.length).toBeGreaterThan(0);
    for (const star of stars) expect(star.className).toBe("al-pin");
  });

  // The touch rules place the star from the row's left, counting what comes
  // before it, and step the callsign in by the star's 12px and its 4px gap
  // (below).
  it("open the callsign, after the indicator and the 13px icon, 12px high with a 4px gap", () => {
    stubViewport(768, { coarse: true });
    const row = renderList().container.querySelector(".al-row")!;
    const [indicator, icon, info] = Array.from(row.children) as HTMLElement[];
    expect(indicator.className).toBe("al-indicator");
    expect(icon.getAttribute("class")).toBe("al-icon");
    expect(icon.style.width).toBe("13px");
    expect(info.className).toBe("al-info");
    const star = info.querySelector(".al-callsign")!.firstElementChild as HTMLElement;
    expect(star.className).toBe("al-pin");
    expect(star.style.fontSize).toBe("12px");
    expect(star.style.lineHeight).toBe("1");
    expect(star.style.padding).toBe("0px");
    expect(star.style.marginRight).toBe("4px");
    // An inline position would outrank the touch rules' and keep the star in
    // the callsign's line, inside its clip.
    expect(star.style.position).toBe("");
  });

  it("step the info column in only on the rows that draw one", () => {
    stubViewport(768, { coarse: true });
    const starred = renderList().container.querySelector(".al-info")!;
    expect(starred.matches(".al-info:has(.al-pin)")).toBe(true);
    const plain = renderList({ pinnable: false }).container;
    expect(plain.querySelector(".al-pin")).toBeNull();
    expect(plain.querySelector(".al-info")!.matches(".al-info:has(.al-pin)")).toBe(false);
  });
});

// ── The stylesheets ──────────────────────────────────────────────────────────

const TOUCH = "@media (max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";
const uncommented = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "");
const sheet = uncommented(css);
const phone = uncommented(phoneCss);

/** The text between the brace that opens after `from` and its partner. */
function braced(text: string, from: number): { inner: string; end: number } {
  const open = text.indexOf("{", from);
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return { inner: text.slice(open + 1, i), end: i + 1 };
  }
  throw new Error(`unbalanced braces after ${from}`);
}

/** The sheet with every @media block taken out: what a desktop draws by. */
function outsideMedia(text: string): string {
  let out = "";
  let at = 0;
  for (let start = text.indexOf("@media"); start >= 0; start = text.indexOf("@media", at)) {
    out += text.slice(at, start);
    at = braced(text, start).end;
  }
  return out + text.slice(at);
}

function declarations(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of body.split(";")) {
    const colon = part.indexOf(":");
    if (colon > 0) out[part.slice(0, colon).trim()] = part.slice(colon + 1).trim();
  }
  return out;
}

/** The declarations of the rule whose selector is exactly `selector`. */
function rule(text: string, selector: string): Record<string, string> | undefined {
  const match = text.match(new RegExp(`(?:^|[}\\s])${selector.replace(/[.:()]/g, "\\$&")}\\s*\\{([^}]*)\\}`));
  return match ? declarations(match[1]) : undefined;
}

/** The rule whose selector list holds `member`: the list and its declarations. */
function ruleWith(text: string, member: string): { selectors: string[]; declarations: Record<string, string> } | undefined {
  for (const [, list, body] of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = list.split(",").map((s) => s.trim());
    if (selectors.includes(member)) return { selectors, declarations: declarations(body) };
  }
  return undefined;
}

/** A length in px. */
const px = (value: string) => Number(/^(-?[\d.]+)px$/.exec(value)![1]);

/** An edge of the shared box, min(0px, calc(50% - Npx)), on a control `size` px across. */
const edge = (value: string, size: number) =>
  Math.min(0, size / 2 - Number(/^min\(0px, calc\(50% - ([\d.]+)px\)\)$/.exec(value)![1]));

const touchStart = sheet.indexOf(TOUCH);
const touch = braced(sheet, touchStart).inner;
const top = outsideMedia(sheet);
const phoneTouch = braced(phone, phone.indexOf(TOUCH)).inner;

describe("the aircraft list's touch rules", () => {
  it("have the phone and touch tablet block, here and in styles/phone.css", () => {
    expect(touchStart).toBeGreaterThanOrEqual(0);
    expect(phone.indexOf(TOUCH)).toBeGreaterThanOrEqual(0);
  });

  it("make the rows 44px tall, as AircraftListPanel spaces them, and leave the desktop's 40px", () => {
    expect(rule(touch, ".al-row")?.height).toBe("44px");
    expect(rule(top, ".al-row")).toMatchObject({ height: "40px", "box-sizing": "border-box" });
  });

  it("place the 12px star against its row, its middle halfway down the row's 44px", () => {
    // The row's padding box starts at its top, the row having no top border,
    // and stops at the 1px bottom border: 16px down, the star's middle is 22px
    // down the row whatever the 43px padding box's own middle.
    expect(rule(touch, ".al-row")).toEqual({ height: "44px", position: "relative" });
    const row = rule(top, ".al-row");
    expect(row).toMatchObject({ "border-bottom": "1px solid var(--border)" });
    expect(row).not.toHaveProperty("border");
    expect(row).not.toHaveProperty("border-top");
    expect(rule(touch, ".al-pin")).toEqual({
      position: "absolute",
      top: "16px",
      left: "40px",
      width: "12px",
      height: "12px",
    });
  });

  it("place the star 40px into the row's padding box, where it opened the callsign", () => {
    // 8px of padding, the 3px indicator, the 13px icon and two 8px gaps; the
    // 2px left border lies outside the padding box.
    expect(rule(top, ".al-row")).toMatchObject({
      gap: "8px",
      padding: "6px 10px 6px 8px",
      "border-left": "2px solid transparent",
    });
    expect(rule(top, ".al-indicator")).toMatchObject({ width: "3px", "flex-shrink": "0" });
  });

  it("give the star the shared 44px box, which spans its row's height and no more", () => {
    const shared = ruleWith(phoneTouch, ".al-pin::after");
    expect(shared?.selectors).toContain(".header-signin::after");
    expect(shared?.declarations).toEqual({
      content: '""',
      position: "absolute",
      top: "min(0px, calc(50% - 22px))",
      right: "min(0px, calc(50% - 22px))",
      bottom: "min(0px, calc(50% - 22px))",
      left: "min(0px, calc(50% - 22px))",
    });
    // The star is placed here, so the box hangs from it and styles/phone.css
    // positions nothing; nor does this sheet lay a box of its own.
    expect(ruleWith(phoneTouch, ".header-signin")?.selectors).not.toContain(".al-pin");
    expect(sheet).not.toMatch(/\.al-pin::after/);

    // From the row's border box, the 2px left border before the padding box:
    // 16px out on every side of the 12px star.
    const star = rule(touch, ".al-pin")!;
    const box = shared!.declarations;
    const [y, x, h, w] = [px(star.top), 2 + px(star.left), px(star.height), px(star.width)];
    expect([y + edge(box.top, h), y + h - edge(box.bottom, h)]).toEqual([0, 44]);
    expect([x + edge(box.left, w), x + w - edge(box.right, w)]).toEqual([26, 70]);
  });

  it("step a row's info column in by its star and the 4px gap, so the callsign keeps its place and the line below lines up with it", () => {
    expect(rule(touch, ".al-info:has(.al-pin)")).toEqual({ "padding-left": "16px" });
    expect(rule(touch, ".al-info")).toBeUndefined();
  });

  it("keep the line below the callsign to one line, cut as the callsign is", () => {
    expect(rule(touch, ".al-sub")).toEqual({ "white-space": "nowrap", overflow: "hidden", "text-overflow": "ellipsis" });
    expect(rule(top, ".al-callsign")).toMatchObject({ "white-space": "nowrap", overflow: "hidden", "text-overflow": "ellipsis" });
  });

  it("leave the info column and the callsign unpositioned, so the star is placed against the row, past the callsign's clip", () => {
    expect(sheet).not.toMatch(/\.al-(info|callsign)[^{},]*\{[^}]*position/);
  });

  it("leave the desktop's star unstyled", () => {
    const everywhere = sheet.match(/\.al-pin\b/g) ?? [];
    const inTouch = touch.match(/\.al-pin\b/g) ?? [];
    expect(inTouch.length).toBeGreaterThan(0);
    expect(everywhere).toHaveLength(inTouch.length);
  });
});
