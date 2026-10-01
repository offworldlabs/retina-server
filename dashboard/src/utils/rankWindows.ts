/**
 * The positions to show of a ranking of `count`, so that each marked position
 * is seen with `reach` of its neighbours either side. Returned as runs of
 * consecutive positions in ranking order; the caller says what lies between
 * two runs.
 *
 * Windows that overlap or meet become one run, and so do two a single
 * position apart: saying that one row was left out would take a row itself.
 */
export function rankWindows(count: number, marked: Iterable<number>, reach: number): number[][] {
  const runs: number[][] = [];
  const positions = [...new Set(marked)].filter((at) => at >= 0 && at < count).sort((a, b) => a - b);
  for (const at of positions) {
    const start = Math.max(0, at - reach);
    const end = Math.min(count - 1, at + reach);
    const run = runs[runs.length - 1];
    const last = run?.[run.length - 1];
    if (run && start <= last + 2) {
      for (let i = last + 1; i <= end; i++) run.push(i);
    } else {
      runs.push(Array.from({ length: end - start + 1 }, (_, i) => start + i));
    }
  }
  return runs;
}
