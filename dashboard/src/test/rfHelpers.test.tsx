import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { defaultChoice, findChoice, nodeChoices } from "../pages/user/rf/choices";
import { NO_SAMPLES, recordSamples, SAMPLE_CAP, type NetworkNode } from "../pages/user/rf/feeds";
import { BucketTooltip } from "../pages/user/rf/SnrCards";
import { clock, hourTicks, isolated, seriesTitle, throughLabel, withGaps } from "../pages/user/rf/series";
import type { SnrSeries } from "../types";
import { CHART_THEMES } from "../utils/chartTheme";
import { ownedNode } from "./fixtures/ownedNode";

const HOUR = 3_600_000;
const STEP = 300_000;
const T0 = Math.floor(new Date("2026-10-13T12:00:00Z").getTime() / STEP) * STEP;

const series = (points: SnrSeries["points"]): SnrSeries => ({ hours: 24, bucket_s: 300, through_ms: null, points });
const point = (t_ms: number, median_db = 10) => ({ t_ms, median_db, p90_db: median_db + 3, count: 1 });

const network = (ref: string, name: string | null, avg: number | null = null): NetworkNode => ({
  ref,
  name,
  frequency: null,
  sampleRate: null,
  location: {},
  summary: avg === null ? null : { metrics: { avg_snr: avg } },
});

describe("an owned node's series as drawn", () => {
  it("adds nothing between buckets that follow on", () => {
    expect(withGaps(series([point(T0), point(T0 + STEP)])).map((b) => b.t_ms)).toEqual([T0, T0 + STEP]);
  });

  it("puts one gap where buckets went missing, however many, saying where it ends", () => {
    const buckets = withGaps(series([point(T0), point(T0 + 5 * STEP)]));
    expect(buckets).toEqual([
      { t_ms: T0, median_db: 10, p90_db: 13, count: 1 },
      { t_ms: T0 + STEP, median_db: null, p90_db: null, count: 0, gap_until: T0 + 5 * STEP },
      { t_ms: T0 + 5 * STEP, median_db: 10, p90_db: 13, count: 1 },
    ]);
  });

  it("orders the points it is given oldest first", () => {
    expect(withGaps(series([point(T0 + STEP), point(T0)])).map((b) => b.t_ms)).toEqual([T0, T0 + STEP]);
  });

  it("draws nothing for no points", () => {
    expect(withGaps(series([]))).toEqual([]);
  });

  it("dots only a bucket with no neighbour to draw a line to", () => {
    const buckets = withGaps(series([point(T0), point(T0 + STEP), point(T0 + 3 * STEP)]));
    expect(buckets.map((_, i) => isolated(buckets, i, "median_db"))).toEqual([false, false, false, true]);
  });
});

describe("the times around a series", () => {
  it("ticks a day's axis on whole hours, three apart", () => {
    const start = T0 - 24 * HOUR;
    const ticks = hourTicks(start, T0);
    expect(ticks.length).toBeGreaterThanOrEqual(8);
    for (const t of ticks) {
      const at = new Date(t);
      expect(at.getMinutes()).toBe(0);
      expect(at.getHours() % 3).toBe(0);
      expect(t).toBeGreaterThanOrEqual(start);
      expect(t).toBeLessThanOrEqual(T0);
    }
  });

  it("gives no ticks for an empty or reversed window", () => {
    expect(hourTicks(T0, T0)).toEqual([]);
    expect(hourTicks(T0, T0 - HOUR)).toEqual([]);
  });

  it("reads a clock on 24 hours", () => {
    expect(clock(T0)).toMatch(/^\d{2}:\d{2}$/);
  });

  it("titles the card with its window and buckets", () => {
    expect(seriesTitle(24, 300)).toBe("SNR, last 24 hours · 5-minute buckets");
    expect(seriesTitle(1, 300)).toBe("SNR, last hour · 5-minute buckets");
  });

  it("says how recent the series is, with the day once it is not today's", () => {
    expect(throughLabel(T0 - 120_000, T0)).toBe(`through ${clock(T0 - 120_000)}`);
    expect(throughLabel(T0 - 48 * HOUR, T0)).toMatch(new RegExp(`^through ${clock(T0 - 48 * HOUR)} on .+`));
  });
});

describe("the live samples", () => {
  it("samples every node that reports an average, and none that does not", () => {
    const history = recordSamples(NO_SAMPLES, { at: T0, nodes: [network("a", "A", 4), network("b", "B")] });
    expect(history.get("a")).toEqual([{ t: T0, snr: 4 }]);
    expect(history.has("b")).toBe(false);
  });

  it("keeps each node's latest samples only", () => {
    let history = NO_SAMPLES;
    for (let i = 0; i < SAMPLE_CAP + 5; i++) {
      history = recordSamples(history, { at: T0 + i, nodes: [network("a", "A", i)] });
    }
    const kept = history.get("a") ?? [];
    expect(kept).toHaveLength(SAMPLE_CAP);
    expect(kept[kept.length - 1]).toEqual({ t: T0 + SAMPLE_CAP + 4, snr: SAMPLE_CAP + 4 });
  });
});

describe("the node picker's choices", () => {
  const mast = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast", status: "disconnected" });
  const roof = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });

  it("offers the owner's nodes by node_id and the rest by ref, an owned node once", () => {
    const choices = nodeChoices(
      [roof, mast],
      [network("nderoof00000001", "roof-node"), network("ndehill00000001", "Hilltop")],
      new Set(["ndemast00000001", "nderoof00000001"]),
    );
    expect(choices.owned.map((c) => [c.value, c.label])).toEqual([
      ["mine:retmast0001", "Mast"],
      ["mine:retroof0001", "Roof · private"],
    ]);
    expect(choices.network.map((c) => [c.value, c.label])).toEqual([["ndehill00000001", "Hilltop"]]);
    expect(findChoice(choices, "ndehill00000001")?.kind).toBe("network");
    expect(findChoice(choices, "ndegone00000001")).toBeNull();
  });

  it("opens on the owner's online node, else their first, else the network's first", () => {
    const listed = [network("ndehill00000001", "Hilltop")];
    expect(defaultChoice(nodeChoices([mast, roof], listed, new Set()))?.value).toBe("mine:retroof0001");
    const offline = { ...roof, status: "never_connected" };
    expect(defaultChoice(nodeChoices([offline, mast], listed, new Set()))?.value).toBe("mine:retmast0001");
    expect(defaultChoice(nodeChoices([], listed, new Set()))?.value).toBe("ndehill00000001");
    expect(defaultChoice(nodeChoices([], [], new Set()))).toBeNull();
  });

  it("tells two nodes of one name apart", () => {
    const choices = nodeChoices(
      [],
      [network("ndeaaaa00000001", "Mast"), network("ndebbbb00000001", "Mast")],
      new Set(),
    );
    expect(choices.network.map((c) => c.label)).toEqual(["Mast · aaaa00000001", "Mast · bbbb00000001"]);
  });
});

describe("a bucket's tooltip", () => {
  const theme = CHART_THEMES.light;

  it("gives the bucket's window, both percentiles and its count", () => {
    const bucket = { t_ms: T0, median_db: 11.04, p90_db: 15.96, count: 1 };
    render(<BucketTooltip active payload={[{ payload: bucket }]} bucketMs={STEP} theme={theme} />);
    expect(screen.getByText(`${clock(T0)}–${clock(T0 + STEP)}`)).toBeInTheDocument();
    expect(screen.getByText("11.0 dB")).toBeInTheDocument();
    expect(screen.getByText("16.0 dB")).toBeInTheDocument();
    expect(screen.getByText("1 detection")).toBeInTheDocument();
  });

  it("gives a gap's whole length and says nothing was detected", () => {
    const gap = { t_ms: T0, median_db: null, p90_db: null, count: 0, gap_until: T0 + 4 * STEP };
    render(<BucketTooltip active payload={[{ payload: gap }]} bucketMs={STEP} theme={theme} />);
    expect(screen.getByText(`${clock(T0)}–${clock(T0 + 4 * STEP)}`)).toBeInTheDocument();
    expect(screen.getByText("No detections")).toBeInTheDocument();
  });

  it("draws nothing while not hovered", () => {
    const { container } = render(<BucketTooltip active={false} payload={[]} bucketMs={STEP} theme={theme} />);
    expect(container).toBeEmptyDOMElement();
  });
});
