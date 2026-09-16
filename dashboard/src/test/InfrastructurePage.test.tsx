import type { ReactNode } from "react";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: { adminInfrastructure: vi.fn() } }));
vi.mock("../utils/chartTheme", () => ({
  useChartTheme: () => ({ grid: "#eee", axis: "#999", tooltip: {}, series: ["#123456"], others: "#999" }),
}));
// Recharts measures a real layout; under jsdom there is none, so the chart
// chrome is stubbed and the page's own text is what gets asserted.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: ReactNode }) => <div data-testid="chart">{children}</div>,
  Area: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
}));

import { api } from "../api/client";
import InfrastructurePage from "../pages/admin/InfrastructurePage";

const unconfigured = {
  configured: false, reason: "DIGITALOCEAN_READ_TOKEN is not set", fetched_at: 1, tag: "retina",
  stale: false, checks: [], droplets: [], errors: [],
};

const configured = {
  configured: true, reason: null, fetched_at: 1789470000, tag: "retina", stale: false,
  checks: [{
    id: "c1", name: "retina-server prod", target: "https://api.retina.fm/api/health", enabled: true,
    status: "UP", uptime_30d: 99.97,
    regions: {
      us_east: { status: "UP", since: "2026-09-01T00:00:00Z", uptime_30d: 99.97 },
      eu_west: { status: "CHECKING", since: "2026-09-05T00:00:00Z", uptime_30d: 98.5 },
      // A since Date.parse cannot read must read as unknown, not as NaN min ago.
      ap_south: { status: "UP", since: "not-a-date", uptime_30d: 99.1 },
    },
    last_outage: { region: "us_east", started_at: "2026-09-10T00:00:00Z", ended_at: "2026-09-10T00:02:00Z", duration_seconds: 120 },
  }, {
    id: "c2", name: "retina-server staging", target: "https://staging-api.retina.fm/api/health", enabled: false,
    status: "UNKNOWN", uptime_30d: null, regions: {}, last_outage: null,
  }],
  droplets: [{
    id: 1, name: "retina-prod", vcpus: 4, memory_mb: 8192, disk_gb: 160, status: "active",
    cpu_pct: 31.2, memory_pct: 44, disk_pct: 18.1,
    series: { cpu: [[1789470000, 31.2]], memory: [[1789470000, 44]], disk: [[1789470000, 18.1]] },
  }, {
    id: 2, name: "retina-staging", vcpus: 2, memory_mb: 4096, disk_gb: 80, status: "off",
    cpu_pct: null, memory_pct: null, disk_pct: null,
    series: { cpu: [], memory: [], disk: [] },
  }],
  errors: ["retina-staging: cpu unavailable (HTTP 403)"],
};

describe("InfrastructurePage", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("says what is missing when the token is not configured", async () => {
    vi.mocked(api.adminInfrastructure).mockResolvedValue(unconfigured);
    render(<InfrastructurePage />);
    expect(await screen.findByText(/DIGITALOCEAN_READ_TOKEN is not set/)).toBeInTheDocument();
    expect(screen.queryByText("retina-server prod")).not.toBeInTheDocument();
  });

  it("renders checks, droplets and degraded items", async () => {
    vi.mocked(api.adminInfrastructure).mockResolvedValue(configured);
    render(<InfrastructurePage />);
    expect(await screen.findByText("retina-server prod")).toBeInTheDocument();
    expect(screen.getByText("99.97%")).toBeInTheDocument();
    expect(screen.getByText("retina-prod")).toBeInTheDocument();
    expect(screen.getByText("31.2%")).toBeInTheDocument();
    expect(screen.getByText(/cpu unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/^as of /)).toBeInTheDocument();
    // CPU, memory and disk, for the one droplet that has series.
    expect(screen.getAllByTestId("chart")).toHaveLength(3);
    const checking = screen.getByText("eu_west CHECKING");
    expect(checking.className).toContain("badge");
    expect(checking.className).toContain("warning");
  });

  it("reads an unparseable region timestamp as unknown", async () => {
    vi.mocked(api.adminInfrastructure).mockResolvedValue(configured);
    render(<InfrastructurePage />);
    const region = await screen.findByText("ap_south UP");
    expect(region.getAttribute("title")).toBe("since unknown");
  });

  it("marks a snapshot the route served past its deadline as stale", async () => {
    vi.mocked(api.adminInfrastructure).mockResolvedValue({ ...configured, stale: true });
    const stale = render(<InfrastructurePage />);
    expect(await screen.findByText("stale")).toBeInTheDocument();
    stale.unmount();

    vi.mocked(api.adminInfrastructure).mockResolvedValue(configured);
    render(<InfrastructurePage />);
    expect(await screen.findByText(/^as of /)).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  it("keeps the last good snapshot when a refresh fails", async () => {
    vi.mocked(api.adminInfrastructure).mockResolvedValueOnce(configured);
    vi.useFakeTimers();
    render(<InfrastructurePage />);
    // The first load resolves as a microtask, before any timer has to run.
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("retina-server prod")).toBeInTheDocument();

    vi.mocked(api.adminInfrastructure).mockRejectedValue(new Error("502 Bad Gateway"));
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.getByText("retina-server prod")).toBeInTheDocument();
    expect(screen.getByText(/502 Bad Gateway/)).toBeInTheDocument();
  });

  it("keeps the newer snapshot when an older request finishes last", async () => {
    vi.useFakeTimers();
    let resolveOlder!: (value: typeof unconfigured) => void;
    vi.mocked(api.adminInfrastructure)
      .mockReturnValueOnce(new Promise((resolve) => { resolveOlder = resolve; }))
      .mockResolvedValueOnce(configured);
    render(<InfrastructurePage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.getByText("retina-server prod")).toBeInTheDocument();
    await act(async () => { resolveOlder(unconfigured); });
    expect(screen.getByText("retina-server prod")).toBeInTheDocument();
  });

  it("distinguishes an off droplet, a disabled check and a metric with no series", async () => {
    vi.mocked(api.adminInfrastructure).mockResolvedValue(configured);
    render(<InfrastructurePage />);
    const off = await screen.findByText("off");
    expect(off.className).toContain("badge");
    expect(off.className).toContain("offline");
    expect(screen.getByText("disabled")).toBeInTheDocument();
    expect(screen.getAllByText("no data")).toHaveLength(3);
  });
});
