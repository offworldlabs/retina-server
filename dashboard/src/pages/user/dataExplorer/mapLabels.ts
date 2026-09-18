/**
 * Naming the dots without stacking the names on top of each other.
 *
 * A label that cannot be placed is dropped rather than overprinted: a dense
 * cluster (the synthetic fleet is 25 nodes inside two degrees) reads as dots
 * with tooltips and does not read at all as a pile of overlapping ids.
 */

import { VIEWPORT } from "./mapProjection";

/** Width of a character in the label's monospaced face, near enough to
 *  reserve space with. */
const CHAR_WIDTH = 6.2;

/** Clear of the dot it names. */
const OFFSET_X = 8;
const OFFSET_Y = -6;

/** One line down, and how close two lines may sit before they collide. */
const NUDGE = 12;
const LINE_HEIGHT = 11;

/** Nudges tried before the label is given up on. */
const MAX_NUDGES = 8;

export interface LabelInput {
  id: string;
  text: string;
  x: number;
  y: number;
}

export interface PlacedLabel extends LabelInput {
  /** Where the text is drawn, which is not where the dot is. */
  labelX: number;
  labelY: number;
}

/**
 * Place what fits, in the order given, and drop the rest. Each label goes to
 * the right of its dot unless that would run off the edge, then down a line at
 * a time until it clears whatever is already there.
 */
export function placeLabels(items: LabelInput[]): PlacedLabel[] {
  const placed: PlacedLabel[] = [];
  const taken: { x: number; y: number; w: number }[] = [];

  for (const item of items) {
    const w = item.text.length * CHAR_WIDTH;
    const labelX =
      item.x + w + OFFSET_X > VIEWPORT.width ? item.x - w - OFFSET_X : item.x + OFFSET_X;
    let labelY = item.y + OFFSET_Y;

    const clashes = () =>
      taken.some(
        (q) => Math.abs(q.y - labelY) < LINE_HEIGHT && labelX < q.x + q.w && labelX + w > q.x,
      );

    for (let tries = 0; tries < MAX_NUDGES && clashes(); tries += 1) labelY += NUDGE;
    if (clashes()) continue;

    taken.push({ x: labelX, y: labelY, w });
    placed.push({ ...item, labelX, labelY });
  }

  return placed;
}
