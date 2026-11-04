import { cloneElement, isValidElement, type ReactNode } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stubViewport } from "../../../test/matchMedia";
import type { NodePerformance } from "../../../types";
import { PerformancePanel } from "./PerformancePanel";

vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: ({ children }: { children: ReactNode }) =>
    isValidElement<{ width?: number; height?: number }>(children)
      ? cloneElement(children, { width: 400, height: 400 })
      : null,
}));

const PERFORMANCE: NodePerformance = {
  schema: 1, window: ["2026-10-28", "2026-11-03"], opportunities: 12345,
  detections: 6000, detections_claims: 4000, detections_tags: 2000, denominator: "aggregator", numerator: "claims",
  system_constant_db: 24.3, system_constant_sigma_db: 0.4, system_constant_bound: "measured", threshold_db: 9.5, pd_ceiling: 0.95,
  pd_by_class_range_alt: { narrowbody: { "0-20km": { "0-5kft": 0.41 }, "20-40km": { "0-5kft": 0.6, "10-20kft": 0.83 } } },
  evidence_by_class_range_alt: { narrowbody: { "0-20km": { "0-5kft": 29 }, "20-40km": { "0-5kft": 30, "10-20kft": 12345 } } },
  tag_delay_residual_mad_us: 0.42, delay_residual_source: "tags", range_50pct_km: { narrowbody: 41.6, widebody: 58 },
  range_50pct_bound: { narrowbody: "measured", widebody: "window" }, classes_with_evidence: ["widebody", "narrowbody"],
  tower: { callsign: "EXAMPLE-TV", eirp_dbm: 50, status: "matched" }, rank_status: null, rank: 4,
};

const fact = (label: string) => screen.getByText(label).closest(".node-performance-fact") as HTMLElement;
const heat = (cell: HTMLElement) => cell.style.getPropertyValue("--node-performance-heat");

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PerformancePanel", () => {
  it("greys a cell under the evidence floor, its count still shown, and heats one at the floor", () => {
    render(<PerformancePanel performance={PERFORMANCE} />);

    const under = screen.getByTitle("Narrowbody, 0–20 km, 0–5 kft: 41% of 29 samples");
    expect(under).toHaveClass("node-performance-cell-greyed");
    expect(heat(under)).toBe("");
    expect(within(under).getByText("29")).toBeInTheDocument();
    const atFloor = screen.getByTitle("Narrowbody, 20–40 km, 0–5 kft: 60% of 30 samples");
    expect(atFloor).not.toHaveClass("node-performance-cell-greyed");
    // A count too wide for a phone's cell, and a key the file leaves out.
    expect(screen.getByTitle("Narrowbody, 20–40 km, 10–20 kft: 83% of 12,345 samples")).toHaveTextContent("83%12k");
    expect(screen.getByTitle("Narrowbody, 60–100 km, 20–45 kft: no figure, 0 samples")).toHaveTextContent("—0");
  });

  it("opens with the default class, keeps a pick, and falls back when a poll stops listing it", () => {
    const { rerender } = render(<PerformancePanel performance={PERFORMANCE} />);
    const pressed = (name: string) => screen.getByRole("button", { name }).getAttribute("aria-pressed");
    expect(pressed("Narrowbody")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Widebody" }));
    expect(pressed("Widebody")).toBe("true");

    rerender(<PerformancePanel performance={{ ...PERFORMANCE, classes_with_evidence: ["narrowbody"] }} />);
    expect(pressed("Narrowbody")).toBe("true");

    rerender(<PerformancePanel performance={{ ...PERFORMANCE, classes_with_evidence: ["bizjet", "widebody"] }} />);
    expect(pressed("Business jet")).toBe("true");
  });

  it("gives way to a sentence when no class has evidence, and the facts and the chart stand", () => {
    const empty = { ...PERFORMANCE, classes_with_evidence: [], pd_by_bearing: Array(72).fill(0.5) };
    render(<PerformancePanel performance={empty} />);

    expect(screen.getByText("No aircraft class has enough evidence for a table yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Aircraft class" })).not.toBeInTheDocument();
    expect(screen.queryByText("50% detection range")).not.toBeInTheDocument();
    expect(fact("Rank")).toHaveTextContent("#4");
    expect(screen.getByText("Detection by bearing")).toBeInTheDocument();
  });

  it("says what bounds each 50% range, and marks the chosen class", () => {
    const performance: NodePerformance = {
      ...PERFORMANCE,
      classes_with_evidence: ["narrowbody", "widebody", "bizjet", "helicopter"],
      range_50pct_km: { narrowbody: 41.6, widebody: 58, bizjet: 58.2, helicopter: null },
      range_50pct_bound: { narrowbody: "measured", widebody: "window", bizjet: "evidence", helicopter: "measured" },
    };
    render(<PerformancePanel performance={performance} />);
    const range = (label: string) => screen.getByText(label, { selector: "dt" }).parentElement as HTMLElement;

    expect(range("Narrowbody").textContent).toBe("Narrowbody42 km");
    expect(range("Narrowbody")).toHaveAttribute("aria-current", "true");
    expect(range("Widebody").textContent).toBe("Widebody≥ 58 km, processing window");
    expect(range("Widebody")).not.toHaveAttribute("aria-current");
    expect(range("Business jet").textContent).toBe("Business jet≥ 58 km, more data needed");
    expect(range("Helicopter").textContent).toBe("Helicopter—");
  });

  it("says what the figures count and that a low-tag node can't be scored yet, for either numerator", () => {
    const rest = "A node that tags few aircraft can't be scored yet: few of its detections count here, so its figures say little about what it sees.";
    const { rerender } = render(<PerformancePanel performance={PERFORMANCE} />);
    expect(screen.getByText(/^These figures count claims/)).toHaveTextContent(`These figures count claims that pass the quality gate. ${rest}`);

    rerender(<PerformancePanel performance={{ ...PERFORMANCE, numerator: "tags" }} />);
    expect(screen.getByText(/^These figures count the node/)).toHaveTextContent(`These figures count the node's own ADS-B tags. ${rest}`);
  });

  it("reads the configuration from claims when it has too few tags, and says so when it has neither", () => {
    const { rerender } = render(<PerformancePanel performance={{ ...PERFORMANCE, delay_residual_source: "claims" }} />);
    expect(fact("Configuration")).toHaveTextContent("Read from its claims, as it has too few tags");

    rerender(<PerformancePanel performance={{ ...PERFORMANCE, tag_delay_residual_mad_us: null, delay_residual_source: null }} />);
    expect(fact("Configuration").textContent).toBe("ConfigurationNot enough tags or claims to check yet");
  });

  it("says a suspect configuration once, under Configuration", () => {
    render(<PerformancePanel performance={{ ...PERFORMANCE, rank: null, rank_status: "config_suspect", tag_delay_residual_mad_us: 3.13 }} />);
    const sentence = "Its detections do not line up with its configured site, so its position or settings need checking";

    expect(fact("Configuration")).toHaveClass("node-performance-fact-warning");
    expect(fact("Configuration").textContent).toBe(`Configuration3.13 µs tag residual${sentence}`);
    expect(fact("Rank").textContent).toBe("RankNot ranked");
  });

  it("names what the residual was read from, and says only that a node with no status isn't ranked", () => {
    const unranked = { ...PERFORMANCE, rank: null, tag_delay_residual_mad_us: 0.3 };
    const { rerender } = render(<PerformancePanel performance={{ ...unranked, delay_residual_source: "claims" }} />);
    expect(fact("Configuration")).toHaveTextContent("0.30 µs claim residual, in line with its site");
    expect(fact("Rank").textContent).toBe("RankNot ranked");

    rerender(<PerformancePanel performance={{ ...unranked, rank_status: "low_ceiling" }} />);
    expect(fact("Configuration")).toHaveTextContent("0.30 µs tag residual, in line with its site");
    expect(fact("Rank").textContent).toBe("RankNot rankedIt misses more than half of even its easiest aircraft, so its sensitivity cannot rank it fairly");
  });

  it("leaves a gap where a bearing has no figure, and a lone bin shows clockwise from north", () => {
    const bins: (number | null)[] = Array(72).fill(null);
    [bins[10], bins[11], bins[12]] = [0.5, 0.6, 0.7];
    const { container, rerender } = render(<PerformancePanel performance={{ ...PERFORMANCE, pd_by_bearing: bins }} />);
    const pen = () => container.querySelector(".recharts-radar-polygon path")?.getAttribute("d")?.match(/[MLZ]/g);

    // One run, left open: nothing joins its ends across the empty bearings.
    expect(pen()).toEqual(["M", "L", "L"]);

    bins[18] = 0.4;
    rerender(<PerformancePanel performance={{ ...PERFORMANCE, pd_by_bearing: [...bins] }} />);
    expect(pen()).toEqual(["M", "L", "L", "M"]);
    const dots = container.querySelectorAll(".recharts-radar-dot");
    expect(dots).toHaveLength(4);
    // 90° is due east of the centre of the 400px chart.
    expect(Number(dots[3].getAttribute("cx"))).toBeGreaterThan(200);
    expect(Number(dots[3].getAttribute("cy"))).toBeCloseTo(200);
  });

  it("on a phone swaps the chips for a select, and a tapped cell opens its detail in a sheet", () => {
    stubViewport(390);
    render(<PerformancePanel performance={PERFORMANCE} />);

    expect(screen.getByRole("combobox", { name: "Aircraft class" })).toHaveValue("narrowbody");
    expect(screen.queryByRole("button", { name: "Narrowbody" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Narrowbody, 0–20 km, 0–5 kft: 41% of 29 samples" }));

    const sheet = within(screen.getByRole("dialog", { name: "Narrowbody" }));
    for (const [label, value] of [["Range", "0–20 km"], ["Altitude", "0–5 kft"], ["Detected", "41%"], ["Samples", "29"]]) {
      expect(sheet.getByText(label).nextElementSibling).toHaveTextContent(value);
    }
    expect(sheet.getByText("Fewer than 30 samples here, so read this as a rough guide")).toBeInTheDocument();
  });

  it("credits the aircraft database under the card, linking it and its licence", () => {
    render(<PerformancePanel performance={PERFORMANCE} />);
    const href = (name: string) => screen.getByRole("link", { name }).getAttribute("href");

    expect(screen.getByText(/^Contains information from the/).textContent).toBe(
      "Contains information from the Mictronics aircraft database, made available under the ODC Attribution License",
    );
    expect(href("Mictronics aircraft database")).toBe("https://github.com/Mictronics/aircraft-database");
    expect(href("ODC Attribution License")).toBe("https://opendatacommons.org/licenses/by/1-0/");
  });
});
