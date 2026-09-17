import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));

/** A listing row for a given UTC day. The day has to track the day being
 *  requested: the page buckets a file by the day in its own key, so a fixture
 *  that always names one day puts every response in one bucket. */
const row = (day: string, node: string, part: string) => ({
  key: `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_id=${node}/${part}.parquet`,
  size_bytes: 2 * 1024 * 1024 * 1024,
  modified: `${day}T06:00:00Z`,
});

/** The day a listing request asked for, back as `YYYY-MM-DD`. */
const dayOf = (url: string) =>
  decodeURIComponent(new URL(url, "http://localhost").searchParams.get("date")).replace(/\//g, "-");

/** One 2 GB file per requested day, on one node. */
const oneFilePerDay = (url: string) =>
  Promise.resolve({ files: [row(dayOf(url), "ret-a", "part-0")], total: 1 });

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
  requestMock.mockResolvedValue({ files: [], total: 0 });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DataExplorerPage", () => {
  it("lists the default range and shows what it found", async () => {
    requestMock.mockImplementation(oneFilePerDay);
    renderAt();

    await waitFor(() => expect(screen.getByText("Files matching")).toBeInTheDocument());
    // Three days in the default range, one file each.
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("3"));
  });

  it("sums sizes in gigabytes", async () => {
    requestMock.mockImplementation(oneFilePerDay);
    renderAt();
    // Three 2 GB files, which the old megabyte-only formatter could not say.
    await waitFor(() => expect(screen.getByTestId("de-stat-bytes")).toHaveTextContent("6.00 GB"));
  });

  it("reads its filters from the query string", async () => {
    renderAt("?from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    const dates = requestMock.mock.calls.map((c) => c[0]);
    expect(dates.every((u: string) => u.includes("date=2026%2F09%2F17"))).toBe(true);
  });

  it("scopes the listing to a single node named in the query string", async () => {
    renderAt("?node=ret-a&from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    expect(requestMock.mock.calls[0][0]).toContain("node_id=ret-a");
  });

  it("writes a filter change back to the query string", async () => {
    renderAt("?from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(screen.getByRole("button", { name: "7d" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() =>
      expect(screen.getByTestId("de-share")).toHaveTextContent("from=2026-09-11"),
    );
  });

  it("says when the range reaches past what is still on disk", async () => {
    // The oldest day of the three is empty, the two after it are not, which is
    // what a retention horizon looks like from the client's side.
    requestMock.mockImplementation((url: string) =>
      dayOf(url) === "2026-09-15" ? Promise.resolve({ files: [], total: 0 }) : oneFilePerDay(url),
    );
    renderAt();
    // Scoped to the notice: the day it names is also a day header below.
    await waitFor(() =>
      expect(screen.getByText(/cold storage/i)).toHaveTextContent("2026-09-16"),
    );
  });

  it("does not claim a horizon while the range is fully populated", async () => {
    requestMock.mockImplementation(oneFilePerDay);
    renderAt();
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("3"));
    expect(screen.queryByText(/cold storage/i)).not.toBeInTheDocument();
  });
});
