import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AvailabilityTimeline } from "../../pages/user/dataExplorer/AvailabilityTimeline";
import explorerCss from "../../pages/user/dataExplorer/dataExplorer.css?raw";
import { defaultFilters } from "../../pages/user/dataExplorer/urlState";
import { stubViewport } from "../matchMedia";
import { timelineProps } from "../stubs/edscTimeline";

/* The availability strip under a finger. The vendored timeline is stubbed (see
   stubs/edscTimeline), so these drive its callbacks and read what the card
   hands it. jsdom lays nothing out, so a test that needs the strip's widths
   sets them itself. */

const TODAY = "2026-09-17";
const DAY = 86_400_000;
const at = (iso: string) => Date.parse(iso);

/** Where the view starts: the middle of the default range, the 15th to the 17th. */
const START = at("2026-09-16T12:00:00Z");

/** Every key the card has always handed the timeline, sorted. */
const PROPS = [
  "center",
  "data",
  "maxZoom",
  "minZoom",
  "onArrowKeyPan",
  "onButtonPan",
  "onButtonZoom",
  "onDragPan",
  "onFocusedSet",
  "onScrollPan",
  "onScrollZoom",
  "onTemporalSet",
  "onTimelineMoveEnd",
  "temporalRange",
  "zoom",
];

function setup(ids = ["ret-a"]) {
  const onChange = vi.fn();
  render(
    <AvailabilityTimeline
      filters={defaultFilters(TODAY)}
      today={TODAY}
      ids={ids}
      files={[]}
      synthetic={() => false}
      anySynthetic={false}
      onChange={onChange}
    />,
  );
  return { onChange };
}

/** The lazy import settles after the first render. */
const timeline = () => screen.findByTestId("edsc-timeline");
const host = () => document.querySelector(".de-tl-host");

/** The strip as a browser lays it out: `width` px inside the host's border,
 *  with the timeline's zoom controls over `covered` px of its left end. */
function layOut(width: number, covered: number) {
  Object.defineProperty(host(), "clientWidth", { configurable: true, value: width });
  host().querySelector(".edsc-timeline-tools")?.remove();
  const tools = document.createElement("section");
  tools.className = "edsc-timeline-tools";
  tools.getBoundingClientRect = () => ({ width: covered }) as DOMRect;
  host().append(tools);
}

function press(name: "Show earlier" | "Show later") {
  const button = screen.queryByRole("button", { name });
  expect(button).toBeInTheDocument();
  fireEvent.click(button);
}

beforeEach(() => {
  timelineProps.last = null;
});

afterEach(() => vi.unstubAllGlobals());

describe("Earlier and Later", () => {
  it("move the view by four fifths of what the strip shows past its zoom controls", async () => {
    stubViewport(390);
    setup();
    const first = await timeline();
    // A quarter covered: 0.8 of the three quarters of a 30-day month showing is 18 days.
    layOut(300, 75);
    expect(timelineProps.last.center).toBe(START);

    press("Show later");
    expect(timelineProps.last.center).toBe(START + 1_555_200_000);
    // Remounted, since the timeline reads `center` only as it mounts, with
    // the zoom and the range it had.
    expect(screen.getByTestId("edsc-timeline")).not.toBe(first);
    expect(timelineProps.last.zoom).toBe(2);
    expect(timelineProps.last.temporalRange).toEqual({
      start: at("2026-09-15T00:00:00Z"),
      end: at("2026-09-18T00:00:00Z"),
    });

    press("Show earlier");
    press("Show earlier");
    expect(timelineProps.last.center).toBe(START - 1_555_200_000);

    // At hour zoom the strip shows a day, of which 0.6 is 14.4 hours.
    act(() => timelineProps.last.onButtonZoom({ center: START, zoom: 1 }));
    press("Show later");
    expect(timelineProps.last.center).toBe(START + 51_840_000);

    // Zoom controls wider than the strip, as a focused day's are on a narrow
    // phone, leave nothing showing: a step is still one interval.
    act(() => timelineProps.last.onButtonZoom({ center: START, zoom: 2 }));
    layOut(228, 256);
    press("Show later");
    expect(timelineProps.last.center).toBe(START + DAY);
  });

  it("step from where the timeline last reported, not from where the last press left it", async () => {
    stubViewport(390);
    setup();
    await timeline();
    layOut(300, 75);

    press("Show later");
    expect(timelineProps.last.center).toBe(START + 1_555_200_000);

    act(() => timelineProps.last.onDragPan({ center: at("2026-08-01T00:00:00Z"), zoom: 2 }));
    press("Show later");
    expect(timelineProps.last.center).toBe(at("2026-08-19T00:00:00Z"));
  });

  it("hide the strip from the press until the timeline reports where it landed", async () => {
    stubViewport(390);
    setup();
    await timeline();
    expect(host()).not.toHaveAttribute("data-moving");

    // Nothing laid out: the whole width counts as shown, 0.8 of a month.
    press("Show later");
    expect(timelineProps.last.center).toBe(START + 24 * DAY);
    expect(host()).toHaveAttribute("data-moving");

    act(() => timelineProps.last.onTimelineMoveEnd({ center: START + 24 * DAY, zoom: 2 }));
    expect(host()).not.toHaveAttribute("data-moving");
  });

  it("show on a tablet driven by touch", async () => {
    stubViewport(820, { coarse: true });
    setup();
    await timeline();
    expect(screen.queryByRole("button", { name: "Show earlier" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show later" })).toBeInTheDocument();
  });

  it.each([820, 1440])(
    "are not drawn at %ipx with a mouse, and the timeline gets what it always has",
    async (width) => {
      stubViewport(width);
      setup();
      await timeline();
      expect(screen.queryByRole("button", { name: /^Show (earlier|later)$/ })).toBeNull();
      expect(Object.keys(timelineProps.last).sort()).toEqual(PROPS);
      expect(timelineProps.last).toMatchObject({
        center: START,
        zoom: 2,
        minZoom: 1,
        maxZoom: 3,
        temporalRange: { start: at("2026-09-15T00:00:00Z"), end: at("2026-09-18T00:00:00Z") },
      });
      expect(host().getAttributeNames()).toEqual(["class"]);
    },
  );
});

describe("the timeline's hint", () => {
  const hint = () => document.querySelector(".de-tl-legend > span:last-child").textContent;

  it("speaks touch on a touch screen", () => {
    stubViewport(390);
    setup(["ret-a", "ret-b", "ret-c", "ret-d"]);
    expect(hint()).toBe(
      "Drag along the top strip to set the date range · tap a date label to focus that day · " +
        "drag the middle or use Earlier and Later to move · ▲▼ to zoom (hour → month). " +
        "The component draws at most 3 rows. Pick up to 3 nodes for one row each.",
    );
  });

  it("keeps the mouse's wording on a desktop", () => {
    stubViewport(1440);
    setup();
    expect(hint()).toBe(
      "Drag along the top strip to set the date range · click a date label to focus that day · " +
        "drag the middle to pan · wheel or ▲▼ to zoom (hour → month). " +
        "The component draws at most 3 rows.",
    );
  });
});

describe("a swipe the browser takes for a page scroll", () => {
  // The timeline's gesture library ends a drag on pointercancel as it does on
  // pointerup, and the timeline reports the range it dragged out there and
  // then. A listener on the stub's element stands in for it, running between
  // the host's capture handler and its bubble handler, as the library's does.
  function cancelOverTopStrip(list: HTMLElement) {
    list.addEventListener("pointercancel", () =>
      timelineProps.last.onTemporalSet({
        temporalStart: at("2026-09-16T05:00:00Z"),
        temporalEnd: at("2026-09-16T05:20:00Z"),
      }),
    );
    fireEvent.pointerCancel(list);
  }

  it("leaves the range alone on a touch screen", async () => {
    stubViewport(390);
    const { onChange } = setup();
    const list = await timeline();
    const drawn = timelineProps.last;

    cancelOverTopStrip(list);
    expect(onChange).not.toHaveBeenCalled();
    // Drawn again, so the timeline shows the filters' range, not the sliver.
    expect(timelineProps.last).not.toBe(drawn);

    // A drag that ends rather than being cancelled still sets it.
    act(() =>
      timelineProps.last.onTemporalSet({
        temporalStart: at("2026-09-10T00:00:00Z"),
        temporalEnd: at("2026-09-12T00:00:00Z"),
      }),
    );
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ from: "2026-09-10", to: "2026-09-11" }));
  });

  it("sets it on a desktop, as it always has", async () => {
    stubViewport(1440);
    const { onChange } = setup();
    cancelOverTopStrip(await timeline());
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ from: "2026-09-16", to: "2026-09-16", todFrom: "05:00", todTo: "05:20" }),
    );
  });
});

describe("the availability strip's touch styles", () => {
  /** Every rule, with the @media prelude around it ("" at the top level). */
  function rulesOf(css: string) {
    const rules: { media: string; selector: string; body: string }[] = [];
    const walk = (text: string, media: string) => {
      let at = 0;
      for (let open = text.indexOf("{"); open >= 0; open = text.indexOf("{", at)) {
        let close = open + 1;
        for (let depth = 1; depth > 0 && close < text.length; close++) {
          depth += text[close] === "{" ? 1 : text[close] === "}" ? -1 : 0;
        }
        const prelude = text.slice(at, open).trim().replace(/\s+/g, " ");
        const body = text.slice(open + 1, close - 1).trim().replace(/\s+/g, " ");
        if (prelude.startsWith("@media")) walk(body, prelude.slice("@media".length).trim());
        else rules.push({ media, selector: prelude, body });
        at = close;
      }
    };
    walk(css.replace(/\/\*[\s\S]*?\*\//g, ""), "");
    return rules;
  }
  const rules = rulesOf(explorerCss);
  const isTouchRule = (r: { selector: string; body: string }) =>
    /\.de-tl-nav|\[data-moving\]/.test(r.selector) || /(^|;)\s*touch-action:/.test(r.body);

  it("styles nothing at 1024px and up", () => {
    const touch = rules.filter(isTouchRule);
    expect(touch.length).toBeGreaterThan(0);
    expect(touch.filter((r) => r.media !== "(max-width: 1023.98px)").map((r) => `${r.media} ${r.selector}`)).toEqual([]);
  });

  // The list is the element the timeline spreads its drag handlers onto.
  it("leaves vertical swipes over the timeline's list to the page", () => {
    const panY = rules.filter((r) => /(^|;)\s*touch-action: pan-y\s*(;|$)/.test(r.body));
    expect(panY.map((r) => [r.media, r.selector])).toEqual([
      ["(max-width: 1023.98px)", ".de-tl-host .edsc-timeline-list"],
    ]);
  });
});
