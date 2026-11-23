import type { ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AnalyticsPage from "../pages/admin/AnalyticsPage";
import { api } from "../api/client";
import { stubViewport } from "./matchMedia";

vi.mock("../api/client", () => ({ api: { analytics: vi.fn(), overlaps: vi.fn() } }));
vi.mock("recharts", () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const Empty = () => null;
  return {
    ResponsiveContainer: Container, LineChart: Empty, Pie: Empty, Cell: Empty, Line: Empty,
    XAxis: Empty, YAxis: Empty, CartesianGrid: Empty, Tooltip: Empty, Legend: Empty,
    BarChart: ({ layout, children }: { layout?: string; children?: ReactNode }) => (
      <div data-testid="trust" data-layout={layout ?? ""}>{children}</div>
    ),
    Bar: ({ radius }: { radius: number[] }) => <output data-testid="radius">{radius.join(",")}</output>,
    PieChart: () => <output data-testid="share" />,
  };
});

// Fifteen nodes: more bars than a phone can label, and more slices than fit one legend row.
const nodes = Object.fromEntries(
  Array.from({ length: 15 }, (_, i) => [
    `nde${String(i).padStart(12, "0")}`,
    { metrics: { total_detections: i + 1 }, trust: { trust_score: 0.5 }, reputation: { reputation: 0.5 } },
  ]),
);

const clientWidth = Object.getOwnPropertyDescriptor(Element.prototype, "clientWidth");

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.analytics).mockResolvedValue({ nodes });
  vi.mocked(api.overlaps).mockResolvedValue([]);
  // jsdom lays nothing out; the wrappers are 340px wide, a phone's card.
  Object.defineProperty(Element.prototype, "clientWidth", { configurable: true, get: () => 340 });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Object.defineProperty(Element.prototype, "clientWidth", clientWidth!);
});

async function wrappers() {
  render(<MemoryRouter><AnalyticsPage /></MemoryRouter>);
  return {
    trust: (await screen.findByTestId("trust")).closest<HTMLElement>(".chart-container")!,
    share: screen.getByTestId("share").closest<HTMLElement>(".chart-container")!,
  };
}

describe("the analytics charts", () => {
  it("lay the trust bars on their side and put the share legend underneath on a phone", async () => {
    stubViewport(390, { coarse: true });
    const { trust, share } = await wrappers();
    expect(screen.getByTestId("trust")).toHaveAttribute("data-layout", "vertical");
    expect(screen.getAllByTestId("radius")[0]).toHaveTextContent("0,4,4,0");
    expect(parseInt(trust.style.height)).toBeGreaterThan(250);
    expect(parseInt(share.style.height)).toBeGreaterThan(250);
  });

  it("draw as they always did on a desktop", async () => {
    stubViewport(1440);
    const { trust, share } = await wrappers();
    expect(screen.getByTestId("trust")).toHaveAttribute("data-layout", "");
    expect(screen.getAllByTestId("radius")[0]).toHaveTextContent("4,4,0,0");
    expect(trust).not.toHaveAttribute("style");
    expect(share).not.toHaveAttribute("style");
  });
});
