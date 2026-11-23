/**
 * The phone and tablet layout rules, measured in the page in one pass.
 *
 * specs/mobile-layout.spec.ts settles a page, then hands probeLayout the width
 * its project emulates and gets back everything that breaks a rule, with the
 * numbers. The width is the project's, never the page's own innerWidth: a
 * mobile browser widens its layout viewport to fit content that overflows,
 * and measuring against that would hide the very overflow being measured.
 *
 * What every rule skips as not drawn: a box under 1 px either way, one with
 * display none or visibility other than visible, and one whose opacity,
 * multiplied down its ancestors, is 0.05 or less (a faded-out layer).
 *
 * The right-edge rule reads a page the way the browser clips it:
 *
 * - An element is cut off where it passes the right edge of the nearest box
 *   that clips it horizontally, or of the screen when none does or that box
 *   is the page itself (html, body, or the console's pane, .content). The
 *   screen's edge also holds inside a clipping box, unless that box sits in a
 *   scroller of its own.
 * - An element whose nearest clipping box is a scroller of its own (overflow-x
 *   auto or scroll, other than the page: a table's wrapper, a code block) can
 *   be scrolled to, and passes.
 * - A box with display: contents draws nothing, so it clips nothing and is
 *   looked through. The map area is one on a phone (pages/map/
 *   LiveAircraftMap.css), and taking its empty 0×0 box for a clipping edge
 *   would report the map's legend and archive bar as cut off by their whole
 *   width.
 * - A positioned element is clipped only by the boxes that contain it: an
 *   absolutely positioned one from its positioned ancestor up, a fixed one by
 *   none but an ancestor that holds fixed boxes (a transform, a filter,
 *   containment).
 * - Only the outermost element of a cut-off subtree is reported.
 *
 * Everything that decides a rule runs in the page, so probeLayout uses
 * nothing from this module but its types.
 */

/** A control the touch-target rule leaves out, and why. */
export interface TouchExemption {
  name: string;
  /** What the control matches: a CSS selector, or a test the probe knows by name. */
  selector?: string;
  test?: "inline text link";
  reason: string;
}

/**
 * The controls the touch-target rule exempts: the whole list. Every other
 * control that is drawn and enabled must take taps across 44 px both ways
 * through its centre (Apple's guidance, WCAG 2.5.5), less the one pixel hit
 * testing may snap away; probeLayout says how that is measured, which
 * controls of a drag-panned strip it leaves for panning, and which map
 * markers it leaves to the other map data drawn over them. A control that is
 * :disabled, inside an [inert] subtree, or itself or inside an element marked
 * aria-disabled="true" cannot be activated, so it is no target and is not
 * counted as exempt.
 */
export const TOUCH_EXEMPTIONS: readonly TouchExemption[] = [
  {
    name: "inline text link",
    test: "inline text link",
    reason:
      "an inline <a> whose block holds other text is a link in a sentence, as tall as its line; " +
      "WCAG 2.5.5 exempts targets inside a sentence",
  },
  {
    name: "map attribution link",
    selector: ".leaflet-control-attribution a",
    reason: "Leaflet's credit line, the small print the basemap's licence asks for",
  },
  {
    name: "timeline date-scale label",
    selector: '.edsc-timeline-interval__interval-bottom[role="button"]',
    reason:
      "the vendored timeline's date scale is dense, its neighbouring labels 10-22 px apart, so they cannot each own 44 px; " +
      "the pages give 44 px ways to move through time instead: the Earlier and Later buttons on the Data Explorer, " +
      "and the archive bar's date and time field and hours toggle",
  },
];

/**
 * What the right-edge rule does not count as cut off, and why. An element
 * past the edge that none of these describes fails.
 */
export const EDGE_EXEMPTIONS = {
  "visually hidden":
    "inside a box of 1 px or less that clips what it holds, the pattern that hides text from sight " +
    "but not from a screen reader (a table's header row on a phone, a second download button)",
  "single-line truncation":
    "cut by a box that ends the line with an ellipsis, which is how a long name is meant to give way",
  "drag-panned strip":
    "inside a box drawn with the grab cursor, a window onto wider content that is panned by dragging " +
    "(the availability timelines, which are clipped this way at every width, the desktop's too)",
  "parked out of sight":
    "positioned or moved wholly past the box that clips it, as a closed off-canvas panel waits; " +
    "if it widened the page, the overflow rule says so",
} as const;

export type EdgeExemption = keyof typeof EDGE_EXEMPTIONS;

export interface ProbeInput {
  /** The screen width the project emulates, in CSS px. */
  width: number;
  /** Checks the fields' font size (phones and touch tablets). */
  fields: boolean;
  /** Checks the table columns hinted hidden (tablets). */
  tablet: boolean;
  exemptions: readonly TouchExemption[];
}

export interface CutOff {
  path: string;
  text: string;
  /** How far its right edge passes the edge that clips it, in px. */
  past: number;
  /** The box whose edge it passes, or null for the screen's. */
  edge: string | null;
  edgeAt: number;
  width: number;
  height: number;
}

export interface TouchMiss {
  path: string;
  text: string;
  box: { width: number; height: number };
  /** How far taps reach either way from the control's centre, summed per axis;
   *  for a map marker, through the other map data drawn over it. */
  hit: { width: number; height: number };
  /** Where the first probe that missed on an axis falling short lay, as
   *  "21.5 px below the centre", and what it landed on. */
  probe: string;
  landedOn: string;
  /** The control lies wholly outside the screen even once scrolled to. */
  outside: boolean;
}

export interface LayoutReport {
  coarsePointer: boolean;
  page: {
    /** The wider of html's and body's scroll widths. */
    scrollWidth: number;
    innerWidth: number;
    /** The visual viewport's width and its scale, below 1 when zoomed out. */
    visual: { width: number; scale: number } | null;
    /** The console's pane, when the page has one. */
    content: { scrollWidth: number; clientWidth: number } | null;
  };
  cutOff: CutOff[];
  edgeExempt: Partial<Record<EdgeExemption, number>>;
  touch: {
    probed: number;
    misses: TouchMiss[];
    /** Controls each touch exemption covered, by its name. */
    exempt: Record<string, number>;
    /** Controls inside a visually hidden box, which no finger can see. */
    notDrawn: number;
    /** Controls of a drag-panned strip lying wholly outside its visible box
     *  once scrolled to, which a finger reaches by panning the strip. */
    pannedOut: number;
    /** Controls of a drag-panned strip whose centre, in the strip's visible
     *  box, lies under a panel drawn over the strip, which a finger reaches by
     *  panning the strip out from under it. */
    underPanel: number;
    /** Leaflet markers probed and found short only where other data of their
     *  map is drawn over them, which lies where the feed puts it. */
    overlapped: number;
  };
  smallFields: { path: string; type: string; px: number }[];
  hiddenColumnsShown: { path: string; text: string; display: string }[];
}

/**
 * Measures the settled page against every rule. Runs in the page.
 *
 * It measures the layout first, then the touch targets, which scrolls each
 * control into view to probe it, so it leaves the page scrolled. Nothing is
 * measured after that.
 */
export function probeLayout(input: ProbeInput): LayoutReport {
  const W = input.width;
  const html = document.documentElement;
  const body = document.body;
  const round = (n: number) => Math.round(n);

  const computed = new Map<Element, CSSStyleDeclaration>();
  const style = (el: Element): CSSStyleDeclaration => {
    let found = computed.get(el);
    if (!found) {
      found = getComputedStyle(el);
      computed.set(el, found);
    }
    return found;
  };

  // An element's tag, id and first three classes, under up to three ancestors.
  const name = (el: Element): string => {
    const classes = (el.getAttribute("class") ?? "").trim().split(/\s+/).filter(Boolean).slice(0, 3);
    return el.tagName.toLowerCase() + (el.id ? `#${el.id}` : "") + (classes.length ? `.${classes.join(".")}` : "");
  };
  const path = (el: Element): string => {
    const parts: string[] = [];
    for (let e: Element | null = el, i = 0; e && e !== body && i < 4; i++, e = e.parentElement) parts.unshift(name(e));
    return parts.join(" > ");
  };
  const label = (el: Element): string =>
    ((el instanceof HTMLElement ? el.innerText : "") || el.getAttribute("aria-label") || el.getAttribute("title") || "")
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 40);

  // ── Drawn ──────────────────────────────────────────────────────────────────
  const opacities = new Map<Element, number>();
  const opacity = (el: Element | null): number => {
    if (!el) return 1;
    let value = opacities.get(el);
    if (value === undefined) {
      const own = Number.parseFloat(style(el).opacity);
      value = (Number.isFinite(own) ? own : 1) * opacity(el.parentElement);
      opacities.set(el, value);
    }
    return value;
  };
  const drawn = (el: Element): boolean => {
    const box = el.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) return false;
    const s = style(el);
    return s.display !== "none" && s.visibility === "visible" && opacity(el) > 0.05;
  };

  const clipsContent = (s: CSSStyleDeclaration): boolean =>
    s.overflowX !== "visible" || s.overflowY !== "visible" || s.clipPath !== "none" || (s.clip !== "" && s.clip !== "auto");
  const hiddenBoxes = new Map<Element, boolean>();
  /** Inside, or itself, a box of 1 px or less that clips what it holds. */
  const visuallyHidden = (el: Element | null): boolean => {
    if (!el || el === body || el === html) return false;
    let value = hiddenBoxes.get(el);
    if (value === undefined) {
      const s = style(el);
      const box = el.getBoundingClientRect();
      value = (s.display !== "contents" && clipsContent(s) && (box.width <= 1 || box.height <= 1)) || visuallyHidden(el.parentElement);
      hiddenBoxes.set(el, value);
    }
    return value;
  };

  // ── Clipping ───────────────────────────────────────────────────────────────
  const isPage = (el: Element): boolean => el === html || el === body || el.matches(".main-area > .content");
  const scrolls = (el: Element): boolean => /^(auto|scroll)$/.test(style(el).overflowX);
  const none = (value: string): boolean => value === "" || value === "none";
  /** Contains fixed boxes: a transform, a filter, containment, a size container. */
  const holdsFixed = (s: CSSStyleDeclaration): boolean =>
    s.transform !== "none" ||
    s.perspective !== "none" ||
    s.filter !== "none" ||
    !none(s.getPropertyValue("backdrop-filter")) ||
    /\b(layout|paint|strict|content)\b/.test(s.getPropertyValue("contain")) ||
    /\b(transform|perspective|filter)\b/.test(s.willChange) ||
    !/^(normal|)$/.test(s.getPropertyValue("container-type"));
  /** The nearest box that clips `el` horizontally, or null for the screen. */
  const clipBox = (el: Element): Element | null => {
    let position = style(el).position;
    for (let a = el.parentElement; a && a !== html; a = a.parentElement) {
      const s = style(a);
      if (s.display === "contents") continue;
      if (position === "fixed" && !holdsFixed(s)) continue;
      if (position === "absolute" && s.position === "static" && !holdsFixed(s)) continue;
      if (s.overflowX !== "visible") return a;
      position = s.position;
    }
    return null;
  };
  const inScroller = (box: Element): boolean => {
    for (let a = clipBox(box); a && !isPage(a); a = clipBox(a)) if (scrolls(a)) return true;
    return false;
  };
  /** Where `box` cuts off what it holds; the screen's edge too, unless a scroller holds the box. */
  const edgeOf = (box: Element | null): number => {
    if (!box || box === body) return W;
    const right = box.getBoundingClientRect().right;
    return !isPage(box) && inScroller(box) ? right : Math.min(W, right);
  };
  /** Moved out of the flow, between `el` and its clipping box. */
  const moved = (el: Element, box: Element | null): boolean => {
    for (let a: Element | null = el; a && a !== box; a = a.parentElement) {
      const s = style(a);
      if (s.position === "absolute" || s.position === "fixed" || s.transform !== "none") return true;
    }
    return false;
  };
  const grabbed = (el: Element): boolean => /grab/.test(style(el).cursor);
  const panned = (el: Element, box: Element | null): boolean => {
    for (let a: Element | null = el; a; a = a.parentElement) {
      if (grabbed(a)) return true;
      if (a === box) break;
    }
    return false;
  };
  const edgeExemption = (el: Element, box: Element | null, left: number, edge: number): EdgeExemption | null => {
    if (visuallyHidden(el)) return "visually hidden";
    if (box && !isPage(box) && style(box).textOverflow === "ellipsis") return "single-line truncation";
    if (panned(el, box)) return "drag-panned strip";
    if (left >= edge - 1 && moved(el, box)) return "parked out of sight";
    return null;
  };

  // ── Overflow ───────────────────────────────────────────────────────────────
  const content = document.querySelector(".main-area > .content");
  const page = {
    scrollWidth: Math.max(html.scrollWidth, body.scrollWidth),
    innerWidth: window.innerWidth,
    visual: window.visualViewport ? { width: window.visualViewport.width, scale: window.visualViewport.scale } : null,
    content: content ? { scrollWidth: content.scrollWidth, clientWidth: content.clientWidth } : null,
  };

  // ── Past the right edge ────────────────────────────────────────────────────
  interface Candidate {
    exemption: EdgeExemption | null;
    cut: CutOff;
  }
  const candidates = new Map<Element, Candidate>();
  for (const el of Array.from(body.querySelectorAll("*"))) {
    if (el instanceof SVGElement && el.tagName.toLowerCase() !== "svg") continue;
    if (el.closest(".leaflet-container") || !drawn(el)) continue;
    const box = clipBox(el);
    if (box && !isPage(box) && scrolls(box)) continue;
    const edge = edgeOf(box);
    const r = el.getBoundingClientRect();
    if (r.right <= edge + 1) continue;
    candidates.set(el, {
      exemption: edgeExemption(el, box, r.left, edge),
      cut: {
        path: path(el),
        text: label(el),
        past: round(r.right - edge),
        edge: box && edge !== W ? path(box) : null,
        edgeAt: round(edge),
        width: round(r.width),
        height: round(r.height),
      },
    });
  }
  const cutOff: CutOff[] = [];
  const edgeExempt: Partial<Record<EdgeExemption, number>> = {};
  for (const [el, { exemption, cut }] of candidates) {
    let outermost = true;
    for (let a = el.parentElement; a && outermost; a = a.parentElement) if (candidates.has(a)) outermost = false;
    if (!outermost) continue;
    if (exemption) edgeExempt[exemption] = (edgeExempt[exemption] ?? 0) + 1;
    else cutOff.push(cut);
  }
  cutOff.sort((a, b) => b.past - a.past);

  // ── Fields under 16 px (iOS zooms the page into one when it takes focus) ───
  const smallFields = !input.fields
    ? []
    : Array.from(document.querySelectorAll("input, select, textarea"))
        .filter((el) => drawn(el) && !/^(checkbox|radio|range|hidden|button|submit|reset|color|file|image)$/.test((el as HTMLInputElement).type || ""))
        .map((el) => ({ path: path(el), type: (el as HTMLInputElement).type || el.tagName.toLowerCase(), px: Number.parseFloat(style(el).fontSize) }))
        .filter((field) => field.px < 16);

  // ── Columns hinted hidden (tablets drop them) ──────────────────────────────
  const hiddenColumnsShown = !input.tablet
    ? []
    : Array.from(document.querySelectorAll('th[data-hint="hidden"], td[data-hint="hidden"]'))
        .filter((cell) => style(cell).display !== "none")
        .map((cell) => ({ path: path(cell), text: label(cell), display: style(cell).display }));

  // ── Touch targets ──────────────────────────────────────────────────────────
  const inlineTextLink = (el: Element): boolean => {
    if (el.tagName !== "A" || style(el).display !== "inline") return false;
    let block = el.parentElement;
    while (block && /^(inline|contents)$/.test(style(block).display)) block = block.parentElement;
    if (!block) return false;
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (el.contains(node) || !/\S/.test(node.nodeValue ?? "") || !parent) continue;
      if (drawn(parent) && !visuallyHidden(parent)) return true;
    }
    return false;
  };
  const exemptionOf = (control: Element): TouchExemption | undefined =>
    input.exemptions.find((exemption) =>
      exemption.selector ? control.matches(exemption.selector) : exemption.test === "inline text link" && inlineTextLink(control),
    );

  const TAPPABLE =
    'a[href], button, input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="tab"], ' +
    '[role="menuitem"], [role="radio"], [role="switch"], [role="checkbox"], [role="option"]';
  // The closed drawer waits wholly off the screen's left edge.
  const drawer = document.getElementById("console-sidebar");
  const drawerClosed = drawer !== null && drawer.getBoundingClientRect().right <= 1;

  const exempt: Record<string, number> = {};
  let notDrawn = 0;
  const seen = new Set<Element>();
  const controls: { control: Element; target: Element }[] = [];
  for (const control of Array.from(document.querySelectorAll(TAPPABLE))) {
    if (control.matches(":disabled") || control.closest("[inert]") || control.closest('[aria-disabled="true"]')) continue;
    if (drawerClosed && drawer.contains(control)) continue;
    // A checkbox or radio is tapped through its label.
    const toggle = control instanceof HTMLInputElement && (control.type === "checkbox" || control.type === "radio");
    const target = toggle ? (control.labels?.[0] ?? control.closest("label") ?? control) : control;
    if (seen.has(target) || !drawn(target)) continue;
    seen.add(target);
    if (visuallyHidden(target)) {
      notDrawn += 1;
      continue;
    }
    const exemption = exemptionOf(control);
    if (exemption) {
      exempt[exemption.name] = (exempt[exemption.name] ?? 0) + 1;
      continue;
    }
    controls.push({ control, target });
  }

  // Each control is probed where it lands once scrolled to, a pixel at a time
  // out from its centre along both axes, up to 22 px each way: how far taps
  // reach before one lands on something else. A probe counts when it lands on
  // the control, inside it, or on one of its labels; an overlay drawn by its
  // ::before or ::after is the control's own. A point past the screen's edge
  // cannot be touched, so it is held on the edge. Nothing is probed off the
  // axes: browsers do not hit-test outside a rounded border, so the corners
  // of the 44×44 square would miss a round 44 px button.
  const STEPS = 22;
  // Taps must reach across 43 px on each axis, not 44: hit testing works on
  // snapped pixels, so a 44 px control at a fractional position loses its
  // outermost probe to the box beside it.
  const MIN_REACH = 43;
  const SIDES: [string, number, number][] = [
    ["above", 0, -1],
    ["below", 0, 1],
    ["left of", -1, 0],
    ["right of", 1, 0],
  ];
  const onScreen = (x: number, y: number): [number, number] => [
    Math.min(Math.max(x, 0.5), window.innerWidth - 0.5),
    Math.min(Math.max(y, 0.5), window.innerHeight - 0.5),
  ];
  const at = (x: number, y: number): Element | null => document.elementFromPoint(...onScreen(x, y));
  // Every scroll lands at once, so the probes measure where it ends: a smooth
  // scroll would only start before the next frame. Set by a style rather
  // than the call's `behavior`, which an engine without "instant" refuses.
  const instant = document.createElement("style");
  instant.textContent = "*, *::before, *::after { scroll-behavior: auto !important; }";
  document.head.append(instant);

  // A drag-panned strip shows a window onto wider content, and a control of
  // it outside that window is reached by panning the strip, which no scroll
  // does. Such a control is counted, not probed; one in the window is probed,
  // unless the point at its centre shows a panel drawn over the strip, a box
  // neither inside the strip nor holding it (the archive bar over the map's
  // markers): where that content sits depends on the data, and panning moves
  // it out from under the panel. The strip's fixed controls do not move with
  // what it pans, so they are probed wherever they lie.
  const FIXED_IN_STRIP = ".leaflet-control";
  /** The strip `el` lies in: the outermost box at or above it drawn with the grab cursor. */
  const stripOf = (el: Element): Element | null => {
    let strip: Element | null = null;
    for (let a: Element | null = el; a && a !== body; a = a.parentElement) if (grabbed(a)) strip = a;
    return strip;
  };
  /** The part of `el` the screen shows: its box, cut by every box that clips it and by the screen. */
  const visibleBox = (el: Element) => {
    const r = el.getBoundingClientRect();
    const box = {
      left: Math.max(r.left, 0),
      top: Math.max(r.top, 0),
      right: Math.min(r.right, window.innerWidth),
      bottom: Math.min(r.bottom, window.innerHeight),
    };
    for (let a = clipBox(el); a; a = clipBox(a)) {
      const c = a.getBoundingClientRect();
      box.left = Math.max(box.left, c.left);
      box.top = Math.max(box.top, c.top);
      box.right = Math.min(box.right, c.right);
      box.bottom = Math.min(box.bottom, c.bottom);
    }
    return box;
  };
  const apart = (r: DOMRect, box: ReturnType<typeof visibleBox>): boolean =>
    Math.min(r.right, box.right) - Math.max(r.left, box.left) < 1 || Math.min(r.bottom, box.bottom) - Math.max(r.top, box.top) < 1;
  const holds = (box: ReturnType<typeof visibleBox>, x: number, y: number): boolean =>
    x >= box.left && x < box.right && y >= box.top && y < box.bottom;

  // A marker of a Leaflet map may fall short just because other data of its
  // map is drawn over it: another marker, a label, a shape or a canvas in a
  // pane above its own. Where that data lies depends on the feed and the
  // moment, so a marker that falls short is probed again, a probe now also
  // counting where the point still holds the marker under that data and
  // nothing else: everything it shows above the marker is inside the map's
  // panned layer, and is neither one of the layer's bare pane boxes nor part
  // of the marker. A marker that reaches far enough that way is overlapped,
  // counted and not reported; one that still falls short is too small by
  // itself, and is reported as the second probing measured it. A probe past
  // the marker's own edge never counts, whatever it lands on: the map's
  // background, or the shapes and canvases drawn beneath the markers.
  const MARKER = ".leaflet-marker-icon";
  const MAP_LAYER = ".leaflet-map-pane";
  type Reach = { side: string; reach: number; hit: Element | null };
  /** How far taps reach out from (cx, cy) on each axis, a probe counting where
   *  `reaches` takes it, and whether either axis falls short. */
  const reachFrom = (cx: number, cy: number, reaches: (hit: Element | null, x: number, y: number) => boolean) => {
    // How far taps reach one way, and what the first probe past that landed on.
    const [above, below, left, right] = SIDES.map(([side, dx, dy]): Reach => {
      for (let k = 1; k <= STEPS; k++) {
        const x = cx + dx * (k - 0.5);
        const y = cy + dy * (k - 0.5);
        const hit = at(x, y);
        if (!reaches(hit, x, y)) return { side, reach: k - 1, hit };
      }
      return { side, reach: STEPS, hit: null };
    });
    // An axis falling short has a side that stopped before STEPS.
    const axis = (a: Reach, b: Reach) => ({ reach: a.reach + b.reach, missed: a.reach < STEPS ? a : b });
    const vertical = axis(above, below);
    const horizontal = axis(left, right);
    return { vertical, horizontal, short: vertical.reach < MIN_REACH || horizontal.reach < MIN_REACH };
  };

  const misses: TouchMiss[] = [];
  let probed = 0;
  let pannedOut = 0;
  let underPanel = 0;
  let overlapped = 0;
  for (const { control, target } of controls) {
    target.scrollIntoView({ block: "center", inline: "nearest" });
    const r = target.getBoundingClientRect();
    const strip = stripOf(target);
    const view = strip ? visibleBox(strip) : null;
    if (view && apart(r, view)) {
      pannedOut += 1;
      continue;
    }
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const own = [control, target, ...Array.from((control as Partial<HTMLInputElement>).labels ?? [])];
    const counts = (hit: Element | null): boolean => hit !== null && own.some((el) => el.contains(hit));
    if (strip && view && holds(view, cx, cy) && !control.closest(FIXED_IN_STRIP) && !target.closest(FIXED_IN_STRIP)) {
      const top = at(cx, cy);
      if (top && !counts(top) && !strip.contains(top) && !top.contains(strip)) {
        underPanel += 1;
        continue;
      }
    }
    probed += 1;
    let reach = reachFrom(cx, cy, counts);
    if (!reach.short) continue;
    const marker = target.closest(MARKER);
    const layer = marker?.closest(MAP_LAYER);
    if (marker && layer) {
      const covered = (x: number, y: number): boolean => {
        const stack = document.elementsFromPoint(...onScreen(x, y));
        const mine = stack.findIndex(counts);
        return mine > 0 && stack.slice(0, mine).every((el) => layer.contains(el) && !el.matches(".leaflet-pane") && !marker.contains(el));
      };
      reach = reachFrom(cx, cy, (hit, x, y) => counts(hit) || covered(x, y));
      if (!reach.short) {
        overlapped += 1;
        continue;
      }
    }
    const { vertical, horizontal } = reach;
    const { missed } = vertical.reach < MIN_REACH ? vertical : horizontal;
    misses.push({
      path: path(target),
      text: label(target),
      box: { width: round(r.width), height: round(r.height) },
      hit: { width: horizontal.reach, height: vertical.reach },
      probe: `${missed.reach + 0.5} px ${missed.side} the centre`,
      landedOn: missed.hit ? path(missed.hit) : "nothing",
      outside: r.right <= 0 || r.bottom <= 0 || r.left >= window.innerWidth || r.top >= window.innerHeight,
    });
  }
  instant.remove();

  return {
    coarsePointer: window.matchMedia("(pointer: coarse)").matches,
    page,
    cutOff,
    edgeExempt,
    touch: { probed, misses, exempt, notDrawn, pannedOut, underPanel, overlapped },
    smallFields,
    hiddenColumnsShown,
  };
}
