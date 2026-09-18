import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";
import { timelineProps } from "../stubs/edscTimeline";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));

const isArchive = (url: string) => url.startsWith("/api/data/archive");

const row = (day: string, node: string) => ({
  key: `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_id=${node}/part-0.parquet`,
  size_bytes: 1024,
  modified: `${day}T06:00:00Z`,
});

const dayOf = (url: string) =>
  decodeURIComponent(new URL(url, "http://localhost").searchParams.get("date") ?? "").replace(
    /\//g,
    "-",
  );

const FLEET = {
  "ret-london": {
    name: "London",
    status: "online",
    is_synthetic: false,
    location: { rx_lat: 51.5074, rx_lon: -0.1278 },
  },
  "ret-oxford": {
    name: "Oxford",
    status: "online",
    is_synthetic: false,
    location: { rx_lat: 51.752, rx_lon: -1.2577 },
  },
};

/** One file per day for each node in the fleet. */
const filesFromBoth = (url: string) =>
  Promise.resolve({
    files: [row(dayOf(url), "ret-london"), row(dayOf(url), "ret-oxford")],
    total: 2,
  });

function serve(nodes: unknown, listing = filesFromBoth) {
  requestMock.mockImplementation((url: string) =>
    isArchive(url)
      ? listing(url)
      : nodes instanceof Error
        ? Promise.reject(nodes)
        : Promise.resolve({ nodes }),
  );
}

function renderAt(search = "") {
  return render(
    <MemoryRouter initialEntries={[`/data${search}`]}>
      <DataExplorerPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-17T12:00:00Z"));
  requestMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DataExplorerPage, node selection and geography", () => {
  it("offers the fleet the radar reports, by name", async () => {
    serve(FLEET);
    renderAt();

    await waitFor(() => expect(screen.getByRole("button", { name: /nodes$/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /nodes$/ }));
    expect(screen.getByRole("checkbox", { name: /London/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Oxford/ })).toBeInTheDocument();
  });

  it("keeps the map out of the way until it is asked for", async () => {
    serve(FLEET);
    renderAt();
    await waitFor(() => expect(screen.getByRole("button", { name: "Map" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Centre on London/ })).not.toBeInTheDocument();
  });

  it("draws a marker for each located node", async () => {
    serve(FLEET);
    renderAt();
    await waitFor(() => expect(screen.getByRole("button", { name: "Map" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(screen.getByRole("button", { name: /Centre on London/ })).toBeInTheDocument();
  });

  it("says how many nodes the fleet has, beside how many have data", async () => {
    serve(FLEET);
    renderAt();
    await waitFor(() => expect(screen.getByText(/of 2 known/)).toBeInTheDocument());
  });

  it("says so when the fleet cannot be reached, rather than showing an empty one", async () => {
    serve(new Error("HTTP 503"));
    renderAt();
    await waitFor(() => expect(screen.getByText(/could not be loaded/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("recovers the fleet on retry", async () => {
    serve(new Error("HTTP 503"));
    renderAt();
    await waitFor(() => expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument());

    serve(FLEET);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => expect(screen.queryByText(/could not be loaded/i)).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(screen.getByRole("button", { name: /Centre on London/ })).toBeInTheDocument();
  });

  it("excludes files from nodes outside the radius", async () => {
    serve(FLEET);
    // Oxford is about 82 km from London, so a 10 km radius drops it.
    renderAt("?from=2026-09-17&to=2026-09-17&near=51.5074,-0.1278,10");

    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("1"));
  });

  it("counts both nodes with no radius, so the exclusion is the filter and not the fixture", async () => {
    serve(FLEET);
    renderAt("?from=2026-09-17&to=2026-09-17");

    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("2"));
  });

  it("keeps a node's file count after it is deselected", async () => {
    serve(FLEET);
    renderAt("?from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("2"));

    fireEvent.click(screen.getByRole("button", { name: /nodes$/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /London/ }));

    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("1"));

    // The panel stays open across the change, so the row is still on screen.
    // Its count is what says whether ticking London again is worth doing, so
    // it has to survive London leaving the selection.
    const row = screen.getByRole("checkbox", { name: /London/ }).closest("label")!;
    expect(within(row).getByText("1")).toBeInTheDocument();
  });

  it("keeps a node's file count even when no file passes the other filters", async () => {
    serve(FLEET);
    // Nothing is a terabyte, so the results are empty while the counts are not.
    renderAt("?from=2026-09-17&to=2026-09-17&minsize=1099511627776");

    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("0"));
    fireEvent.click(screen.getByRole("button", { name: /nodes$/ }));
    const row = screen.getByRole("checkbox", { name: /London/ }).closest("label")!;
    expect(within(row).getByText("1")).toBeInTheDocument();
  });

  it("draws the timeline from the selection, and moves the filters when it is dragged", async () => {
    serve(FLEET);
    renderAt("?node=ret-london&from=2026-09-17&to=2026-09-17");

    const drawn = await screen.findByTestId("edsc-timeline");
    await waitFor(() => expect(drawn).toHaveTextContent("ret-london: 1"));
    expect(drawn).not.toHaveTextContent("ret-oxford");

    act(() =>
      timelineProps.last.onTemporalSet({
        temporalStart: Date.parse("2026-09-12T00:00:00Z"),
        temporalEnd: Date.parse("2026-09-14T00:00:00Z"),
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("de-share")).toHaveTextContent(
        "?node=ret-london&from=2026-09-12&to=2026-09-13",
      ),
    );
  });
});
