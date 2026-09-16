import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AnalyticsPage from "../pages/admin/AnalyticsPage";
import NetworkHealthPage from "../pages/admin/NetworkHealthPage";
import RFEnvironmentPage from "../pages/user/RFEnvironmentPage";
import { api } from "../api/client";

vi.mock("../api/client", () => ({ api: {
  analytics: vi.fn(), aircraft: vi.fn(), nodes: vi.fn(), overlaps: vi.fn(),
  fleetDashboard: vi.fn(), adminNodeRefs: vi.fn(),
} }));
vi.mock("react-leaflet", async (importOriginal) => ({
  ...await importOriginal<typeof import("react-leaflet")>(), MapContainer: () => null,
}));
vi.mock("recharts", () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const History = ({ data }) => <output data-testid="history">{JSON.stringify(data)}</output>;
  const Empty = () => null;
  return {
    ResponsiveContainer: Container, AreaChart: History, LineChart: History,
    BarChart: Empty, PieChart: Empty, Bar: Empty, Pie: Empty, Cell: Empty,
    Line: Empty, Area: Empty, XAxis: Empty, YAxis: Empty, CartesianGrid: Empty,
    Tooltip: Empty, Legend: Empty,
  };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}
const analytics = (value: number) => ({ nodes: {
  a: { metrics: { total_detections: value, avg_snr: value } },
  b: { metrics: { total_detections: 0, avg_snr: value + 10 } },
} });
const aircraft = (value: number) => ({ aircraft: Array.from({ length: value }, (_, i) => ({ hex: `ac${i}` })) });
const cases = [
  { name: "analytics", Page: AnalyticsPage, method: "analytics", snapshot: analytics, interval: 10_000, field: "detections", cap: 30 },
  { name: "network", Page: NetworkHealthPage, method: "aircraft", snapshot: aircraft, interval: 5000, field: "aircraft", cap: 30 },
  { name: "RF", Page: RFEnvironmentPage, method: "analytics", snapshot: analytics, interval: 5000, field: "snr", cap: 31 },
] as const;
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const history = () => JSON.parse(screen.getByTestId("history").textContent!);

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  vi.mocked(api.nodes).mockResolvedValue({ nodes: { a: { name: "Node A" }, b: { name: "Node B" } } });
  vi.mocked(api.analytics).mockResolvedValue(analytics(1));
  vi.mocked(api.aircraft).mockResolvedValue(aircraft(1));
  vi.mocked(api.overlaps).mockResolvedValue([]);
  vi.mocked(api.fleetDashboard).mockResolvedValue({});
  vi.mocked(api.adminNodeRefs).mockResolvedValue({});
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe.each(cases)("$name polling history", ({ Page, method, snapshot, interval, field, cap }) => {
  it("does not append a stale sample when an earlier request finishes last", async () => {
    render(<MemoryRouter><Page /></MemoryRouter>);
    await tick(0);
    await tick(interval);
    const earlier = deferred<object>();
    vi.mocked(api[method]).mockReturnValueOnce(earlier.promise);
    await tick(interval);
    vi.mocked(api[method]).mockResolvedValueOnce(snapshot(9));
    await tick(interval);
    const committed = history();
    expect(committed[committed.length - 1][field]).toBe(9);
    await act(async () => earlier.resolve(snapshot(4)));
    expect(history()).toEqual(committed);
  });

  it("keeps the existing history window bound", async () => {
    render(<MemoryRouter><Page /></MemoryRouter>);
    await tick(0);
    for (let i = 0; i < 35; i++) await tick(interval);
    expect(history()).toHaveLength(cap);
  });
});

it("does not let a delayed initial RF fetch replace a user's selection or history", async () => {
  const earlier = deferred<object>();
  vi.mocked(api.analytics).mockReturnValueOnce(earlier.promise);
  render(<MemoryRouter><RFEnvironmentPage /></MemoryRouter>);
  await tick(5000);
  expect(screen.getByRole("combobox")).toHaveValue("a");
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "b" } });
  await tick(0);
  const committed = history();
  expect(committed[committed.length - 1].snr).toBe(11);
  await act(async () => earlier.resolve(analytics(4)));
  expect(screen.getByRole("combobox")).toHaveValue("b");
  expect(history()).toEqual(committed);
});
