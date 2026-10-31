import { afterEach, describe, expect, it } from "vitest";
import barCss from "./ArchiveBar.css?raw";

const HOUR_MS = 3_600_000;
const added: Element[] = [];

afterEach(() => {
  for (const node of added.splice(0)) node.remove();
});

/** Adds ArchiveBar.css as a viewport `width` px wide applies it, and returns
 *  the conditions of the media blocks that hold at that width. */
function applyBarCss(width: number): string[] {
  // jsdom matches no media query with a media feature, so each block whose
  // width condition holds has its rules copied out to the top level, in place.
  const style = document.head.appendChild(document.createElement("style"));
  added.push(style);
  style.textContent = barCss;
  const applied: string[] = [];
  for (let i = style.sheet.cssRules.length - 1; i >= 0; i--) {
    const rule = style.sheet.cssRules[i];
    if (!(rule instanceof CSSMediaRule) || !holds(rule.media.mediaText, width)) continue;
    applied.unshift(rule.media.mediaText);
    [...rule.cssRules].forEach((inner, k) => style.sheet.insertRule(inner.cssText, i + 1 + k));
  }
  return applied;
}

/** `(max-width: Npx)` or `(min-width: Npx)`; any other condition is left as jsdom leaves it. */
function holds(condition: string, width: number): boolean {
  const [, bound, px] = /^\((max|min)-width:\s*(\d+(?:\.\d+)?)px\)$/.exec(condition) ?? [];
  if (bound === "max") return width <= Number(px);
  if (bound === "min") return width >= Number(px);
  return false;
}

/** The strip's hour view as @edsc/timeline renders it: 30 hours, both zoom
 *  buttons enabled, and each hour's section label empty but at 00:00, where it
 *  holds the date. Midnight is unit `midnightAt`. */
function hourView(midnightAt: number): HTMLElement {
  const start = Date.UTC(2026, 9, 26) - (midnightAt - 1) * HOUR_MS;
  const units = Array.from({ length: 30 }, (_, i) => {
    const at = new Date(start + i * HOUR_MS);
    const time = `${String(at.getUTCHours()).padStart(2, "0")}:00`;
    const date = time === "00:00" ? at.toUTCString().slice(5, 16) : "";
    return `<div class="edsc-timeline-interval" data-testid="timelineInterval"><div class="edsc-timeline-interval__interval-top"><span class="edsc-timeline-interval__interval-section-label">${date}</span></div><div class="edsc-timeline-interval__interval-bottom" role="button" aria-label="Focus interval" aria-disabled="false" tabindex="0" data-testid="timelineInterval-${30 - i}"><span class="edsc-timeline-interval__interval-label">${time}</span></div></div>`;
  });
  const host = document.body.appendChild(document.createElement("div"));
  added.push(host);
  host.className = "archive-strip-host";
  host.innerHTML = `<div class="edsc-timeline" data-testid="timeline"><div class="edsc-timeline__tooltips"><div class="edsc-timeline__tooltip" data-testid="tooltip"></div></div><section class="edsc-timeline-tools"><section class="edsc-timeline-tools__section"><button class="edsc-timeline-tools__action" type="button" aria-label="Increase zoom level" data-testid="zoomUp"></button><span aria-label="Zoom level" class="edsc-timeline-tools__label edsc-timeline-tools__label--resolution">Hour</span><button class="edsc-timeline-tools__action" type="button" aria-label="Decrease zoom level" data-testid="zoomDown"></button></section></section><section class="edsc-timeline-primary-section"><ul class="edsc-timeline-primary-section__entries"></ul></section><div class="edsc-timeline__outer-wrapper"><div class="edsc-timeline__wrapper"><div class="edsc-timeline-list" role="button" tabindex="0" data-testid="timelineList"><section class="edsc-timeline-list__markers"></section><span class="edsc-timeline-list__line"></span><div class="edsc-timeline-data-section"></div><div class="edsc-timeline-list__intervals">${units.join("")}</div></div></div></div></div>`;
  return host;
}

/** The text of each hour label drawn, in order. */
function shownLabels(host: HTMLElement): string[] {
  const labels = [...host.querySelectorAll(".edsc-timeline-interval__interval-label")];
  expect(labels).toHaveLength(30);
  return labels.filter((label) => getComputedStyle(label).display !== "none").map((label) => label.textContent);
}

describe("the strip's hour labels", () => {
  it.each([1, 2, 3, 4, 5, 6])("on a phone, label only 00, 06, 12 and 18 UTC, with midnight at unit %i", (midnightAt) => {
    expect(applyBarCss(390)).toContainEqual(expect.stringMatching(/^\(max-width: \d+px\)$/));
    expect(shownLabels(hourView(midnightAt))).toEqual(["00:00", "06:00", "12:00", "18:00", "00:00"]);
  });

  it("on a desktop, label every hour", () => {
    applyBarCss(1280);
    expect(shownLabels(hourView(1))).toHaveLength(30);
  });
});
