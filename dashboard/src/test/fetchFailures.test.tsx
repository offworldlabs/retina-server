import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentType } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* Every console page that fetches says so when the fetch fails, rather than
   drawing its empty state as though there were nothing to show. */

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { name: "Ada", role: "admin" }, loading: false }),
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

type Answer = { status: number; body: unknown };
let answer: (path: string) => Answer = () => ({ status: 500, body: {} });

beforeEach(() => {
  answer = () => ({ status: 500, body: {} });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const { status, body } = answer(String(path));
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }),
  );
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

type Page = () => Promise<{ default: ComponentType }>;

const PAGES: [string, Page, string][] = [
  ["Analytics", () => import("../pages/admin/AnalyticsPage"), "analytics"],
  ["Custody", () => import("../pages/admin/CustodyPage"), "custody records"],
  ["Events", () => import("../pages/admin/EventsPage"), "events"],
  ["Infrastructure", () => import("../pages/admin/InfrastructurePage"), "the infrastructure snapshot"],
  ["MLAT Verification", () => import("../pages/admin/MlatVerificationPage"), "MLAT verification"],
  ["Network Health", () => import("../pages/admin/NetworkHealthPage"), "network health"],
  ["Node Management", () => import("../pages/admin/NodeManagementPage"), "the node list"],
  ["Storage", () => import("../pages/admin/StoragePage"), "storage figures"],
  ["Storage archive", () => import("../pages/admin/StoragePage"), "the archive listing"],
  ["System Metrics", () => import("../pages/admin/SystemMetricsPage"), "system metrics"],
  ["Alerts", () => import("../pages/user/AlertsPage"), "alerts"],
  ["Anomaly", () => import("../pages/user/AnomalyPage"), "anomaly data"],
  ["Contribution", () => import("../pages/user/ContributionPage"), "network contribution"],
  ["Detections", () => import("../pages/user/DetectionsPage"), "detections"],
  ["Leaderboard", () => import("../pages/user/LeaderboardPage"), "the leaderboard"],
  ["Onboarding", () => import("../pages/user/OnboardingPage"), "your nodes"],
  ["Overview", () => import("../pages/user/OverviewPage"), "your nodes"],
  ["RF Environment", () => import("../pages/user/RFEnvironmentPage"), "RF environment data"],
  ["Tunnel Link", () => import("../pages/user/TunnelLinkPage"), "your nodes"],
];

async function renderPage(load: Page) {
  const { default: Page } = await load();
  return render(
    <MemoryRouter>
      <Page />
    </MemoryRouter>,
  );
}

describe("a console page whose first fetch fails", () => {
  it.each(PAGES)("%s shows the error notice in place of its content", async (_, load, what) => {
    await renderPage(load);
    const notices = await screen.findAllByRole("alert");
    const notice = notices.find((n) => n.textContent?.includes(`Could not load ${what}`));
    expect(notice).toBeDefined();
    expect(notice).toHaveClass("notice", "error");
    // The content it replaces: no table, no stat tiles standing in with zeroes.
    expect(document.querySelector(".stat-card")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("names the page above the notice", async () => {
    await renderPage(() => import("../pages/user/AlertsPage"));
    await screen.findByRole("alert");
    expect(screen.getByRole("heading", { name: "Alerts & Notifications" })).toBeInTheDocument();
    expect(screen.queryByText(/No alerts/)).toBeNull();
  });

  it("tells a missing node apart from a failed fetch", async () => {
    const { default: NodeDetailPage } = await import("../pages/user/NodeDetailPage");
    const at = (status: number) => {
      answer = () => ({ status, body: {} });
      return render(
        <MemoryRouter initialEntries={["/nodes/nde0example0001"]}>
          <Routes>
            <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
          </Routes>
        </MemoryRouter>,
      );
    };
    const failed = at(502);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load this node");
    expect(screen.queryByText("Node not found")).toBeNull();
    // The way back survives the failure.
    expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument();
    failed.unmount();

    at(404);
    expect(await screen.findByText("Node not found")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("a console page whose refresh fails", () => {
  const alert = { ts: 1790000000, severity: "warning", category: "node", message: "ret-abc went quiet" };

  it.each([
    ["Alerts", () => import("../pages/user/AlertsPage"), "alerts", "/api/admin/alerts", [alert], "ret-abc went quiet"],
    [
      "System Metrics",
      () => import("../pages/admin/SystemMetricsPage"),
      "system metrics",
      "/api/admin/metrics",
      { frames_processed: 4242, task_last_success: {}, task_error_counts: {} },
      "4,242",
    ],
    [
      "Anomaly",
      () => import("../pages/user/AnomalyPage"),
      "anomaly data",
      "/api/radar/anomalies",
      { summary: { active_count: 7 }, by_type: {}, timeline: [], geographic_clusters: [], recent_events: [] },
      "7",
    ],
  ] as [string, Page, string, string, unknown, string][])(
    "%s keeps what it showed and says how old it is",
    async (_, load, what, route, body, shown) => {
      vi.useFakeTimers();
      answer = (path) => (path.startsWith(route) ? { status: 200, body } : { status: 500, body: {} });
      await renderPage(load);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText(shown)).toBeInTheDocument();
      expect(screen.queryByRole("alert")).toBeNull();

      answer = () => ({ status: 502, body: {} });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(screen.getByText(shown)).toBeInTheDocument();
      const notice = screen.getByRole("alert");
      expect(notice.className).toBe("notice");
      expect(notice).toHaveTextContent(`Could not refresh ${what}`);
      expect(notice).toHaveTextContent("Showing what was loaded at");
    },
  );

  it("marks Anomaly's event table stale too, far below the notice", async () => {
    vi.useFakeTimers();
    const body = { summary: {}, by_type: {}, timeline: [], geographic_clusters: [], recent_events: [] };
    answer = (path) => (path.startsWith("/api/radar/anomalies") ? { status: 200, body } : { status: 500, body: {} });
    await renderPage(() => import("../pages/user/AnomalyPage"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.queryByText("stale")).toBeNull();

    answer = () => ({ status: 502, body: {} });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(screen.getByText("stale")).toHaveClass("badge", "warning");
  });
});

describe("a keyed page whose next page fails", () => {
  it("does not show the previous page's rows as this page's", async () => {
    const first = { key: "year=2026/month=09/day=22/node_id=ret-a/first-page.json", size_bytes: 1 };
    answer = (path) =>
      path.startsWith("/api/data/archive") && path.includes("offset=0")
        ? { status: 200, body: { files: [first], total: 120 } }
        : path.startsWith("/api/admin/storage")
          ? { status: 200, body: {} }
          : { status: 502, body: {} };
    await renderPage(() => import("../pages/admin/StoragePage"));
    expect(await screen.findByText("first-page.json")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(await screen.findByText(/Could not load the archive listing/)).toBeInTheDocument();
    expect(screen.queryByText("first-page.json")).toBeNull();
    expect(screen.getByRole("button", { name: /prev/i })).toBeInTheDocument();
  });
});

describe("a console page still loading", () => {
  it("says it is loading rather than drawing its empty state", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    await renderPage(() => import("../pages/admin/StoragePage"));
    await waitFor(() => expect(screen.getAllByText("Loading…").length).toBeGreaterThan(0));
    expect(screen.queryByText("No disk data")).toBeNull();
    expect(screen.queryByText("No write rate data")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
