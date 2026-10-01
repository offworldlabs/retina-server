import { describe, it, expect } from "vitest";
import { rankWindows } from "../utils/rankWindows";

describe("rankWindows", () => {
  it("shows the positions either side of a marked one", () => {
    expect(rankWindows(20, [10], 2)).toEqual([[8, 9, 10, 11, 12]]);
  });

  it("stops at either end of the ranking", () => {
    expect(rankWindows(20, [0], 2)).toEqual([[0, 1, 2]]);
    expect(rankWindows(20, [19], 2)).toEqual([[17, 18, 19]]);
    expect(rankWindows(2, [0], 2)).toEqual([[0, 1]]);
  });

  it("merges windows that overlap or meet", () => {
    expect(rankWindows(20, [4, 6], 2)).toEqual([[2, 3, 4, 5, 6, 7, 8]]);
    expect(rankWindows(20, [4, 9], 2)).toEqual([[2, 3, 4, 5, 6, 7, 8, 9, 10, 11]]);
  });

  // Saying that one row was left out takes a row, so the row itself is shown.
  it("fills a gap of a single position", () => {
    expect(rankWindows(20, [4, 10], 2)).toEqual([[2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]]);
  });

  it("keeps windows apart across a gap of two or more", () => {
    expect(rankWindows(20, [4, 11], 2)).toEqual([
      [2, 3, 4, 5, 6],
      [9, 10, 11, 12, 13],
    ]);
  });

  it("takes the marked positions in any order, repeated or not", () => {
    expect(rankWindows(20, [15, 2, 15], 1)).toEqual([
      [1, 2, 3],
      [14, 15, 16],
    ]);
  });

  it("ignores a position outside the ranking", () => {
    expect(rankWindows(5, [-1, 5], 2)).toEqual([]);
  });

  it("has nothing to show with nothing marked", () => {
    expect(rankWindows(20, [], 2)).toEqual([]);
  });
});
