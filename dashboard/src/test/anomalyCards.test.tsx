import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import AnomalyPage from "../pages/user/AnomalyPage";
import { stubViewport } from "./matchMedia";

vi.mock("../api/client", () => ({
  api: { anomalies: vi.fn(), myNodes: vi.fn(), myAircraft: vi.fn() },
}));
vi.mock("../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

// Recharts stubs that keep each axis's props and draw their children.
const axes = vi.hoisted(() => ({ y: [] as Record<string, unknown>[] }));
vi.mock("recharts", async () => {
  const pass = ({ children }: { children?: React.ReactNode }) => <>{children}</>;
  return {
    ResponsiveContainer: pass,
    AreaChart: pass,
    BarChart: pass,
    Area: pass,
    Bar: pass,
    Cell: () => null,
    CartesianGrid: () => null,
    Tooltip: () => null,
    XAxis: () => null,
    YAxis: (props: Record<string, unknown>) => {
      axes.y.push(props);
      return null;
    },
  };
});

const X = { hex: "ee0001", reason: "anomalous_behavior", lat: 51.5, lon: -0.1, ts: 1760000000 };
const NEWER = { hex: "ee0002", reason: "anomalous_behavior", lat: 51.6, lon: -0.2, ts: 1760000100 };

const data = (events: (typeof X)[]) => ({
  summary: { active_count: 1, total_events: events.length, unique_hexes: events.length, most_common_type: "anomalous_behavior" },
  by_type: { anomalous_behavior: 4, impossible_speed: 2 },
  timeline: [{ ts: 1760000000, count: 2 }],
  geographic_clusters: [{ lat: 51.5, lon: -0.1, count: 3, dominant_type: "anomalous_behavior" }],
  recent_events: events,
});

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

async function show() {
  const { unmount } = render(
    <MemoryRouter>
      <AnomalyPage />
    </MemoryRouter>,
  );
  await settle();
  return unmount;
}

/** The width the breakdown's category axis was last drawn with. */
const categoryWidth = () => {
  const drawn = axes.y.filter((p) => p.type === "category");
  return drawn[drawn.length - 1]?.width;
};

const rowOf = (text: string) => screen.getByText(text).closest("tr")!;

describe("AnomalyPage as cards", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
    axes.y.length = 0;
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("heads an event's card with its hex and a hotspot's with its location", async () => {
    stubViewport(390);
    vi.mocked(api.anomalies).mockResolvedValue(data([X]));
    await show();

    expect(rowOf("ee0001").querySelector('td[data-label="Hex"]')).toHaveAttribute("data-hint", "title");
    expect(rowOf("51.5, -0.1").querySelector('td[data-label="Location"]')).toHaveAttribute("data-hint", "title");
  });

  it("keeps an event's card open when a newer event arrives after it", async () => {
    stubViewport(390);
    vi.mocked(api.anomalies).mockResolvedValue(data([X]));
    await show();

    fireEvent.click(rowOf("ee0001").querySelector("button")!);
    expect(rowOf("ee0001")).toHaveAttribute("data-card-open");

    vi.mocked(api.anomalies).mockResolvedValue(data([X, NEWER]));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    await settle();

    expect(rowOf("ee0001")).toHaveAttribute("data-card-open");
    expect(rowOf("ee0002")).not.toHaveAttribute("data-card-open");
  });

  it("caps the breakdown's label column at 40% of a phone chart's width, and leaves a desktop's at 140", async () => {
    stubViewport(390);
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(300);
    vi.mocked(api.anomalies).mockResolvedValue(data([X]));
    const unmount = await show();

    expect(categoryWidth()).toBe(120);

    unmount();
    axes.y.length = 0;
    stubViewport(1280);
    await show();

    expect(categoryWidth()).toBe(140);
  });
});
