import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";
import { stubViewport } from "../matchMedia";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));
// A visitor: the page as it is to anyone signed out, with no owner calls.
// DataExplorerOwner.test.tsx has the signed-in owner's.
vi.mock("../../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

/** A listing row for a given UTC day. The day has to track the day being
 *  requested: the page buckets a file by the day in its own key, so a fixture
 *  that always names one day puts every response in one bucket. */
const row = (day: string, node: string, part: string) => ({
  key: `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_ref=${node}/${part}.parquet`,
  size_bytes: 2 * 1024 * 1024 * 1024,
  modified: `${day}T06:00:00Z`,
});

/** The day a listing request asked for, back as `YYYY-MM-DD`. */
const dayOf = (url: string) =>
  decodeURIComponent(new URL(url, "http://localhost").searchParams.get("date")).replace(/\//g, "-");

/** One 2 GB file per requested day, on one node. */
const oneFilePerDay = (url: string) =>
  Promise.resolve({ files: [row(dayOf(url), "ret-a", "part-0")], total: 1 });

/** The page fetches the node registry as well as listings, and only listings
 *  carry a `date`, so fixtures have to tell the two apart. */
const isArchive = (url: string) => url.startsWith("/api/data/archive");

const archiveCalls = () => requestMock.mock.calls.map((c) => c[0] as string).filter(isArchive);

const withRegistry =
  (listing: (url: string) => Promise<unknown>, nodes: Record<string, unknown> = {}) =>
  (url: string) =>
    isArchive(url) ? listing(url) : Promise.resolve({ nodes });

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
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("DataExplorerPage", () => {
  it("lists the default range and shows what it found", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay));
    renderAt();

    await waitFor(() => expect(screen.getByText("Files matching")).toBeInTheDocument());
    // Three days in the default range, one file each.
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("3"));
  });

  it("sums sizes in gigabytes", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay));
    renderAt();
    // Three 2 GB files, which the old megabyte-only formatter could not say.
    await waitFor(() => expect(screen.getByTestId("de-stat-bytes")).toHaveTextContent("6.00 GB"));
  });

  it("reads its filters from the query string", async () => {
    renderAt("?from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(archiveCalls().length).toBeGreaterThan(0));
    expect(archiveCalls().every((u) => u.includes("date=2026%2F09%2F17"))).toBe(true);
  });

  it("scopes the listing to a single node named in the query string, once the radar publishes it", async () => {
    let fleet!: (nodes: unknown) => void;
    requestMock.mockImplementation((url: string) =>
      isArchive(url) ? oneFilePerDay(url) : new Promise((resolve) => (fleet = resolve)),
    );
    renderAt("?node=ret-a&from=2026-09-17&to=2026-09-17");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    // Until the radar answers, the node could be one it keeps private.
    expect(archiveCalls()).toEqual([]);

    await act(async () => fleet({ nodes: { "ret-a": { name: "A" } } }));
    await waitFor(() => expect(archiveCalls().length).toBeGreaterThan(0));
    expect(archiveCalls()).toEqual(["/api/data/archive?date=2026%2F09%2F17&limit=500&offset=0&node_ref=ret-a"]);
  });

  it("lists the whole day for a node the radar does not publish, and picks it out of that", async () => {
    // ret-a has files in the archive, but the radar no longer lists it.
    requestMock.mockImplementation(withRegistry(oneFilePerDay, { "ret-b": { name: "B" } }));
    renderAt("?node=ret-a&from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("1"));
    expect(archiveCalls()).toEqual(["/api/data/archive?date=2026%2F09%2F17&limit=500&offset=0"]);
    // Its own files named it, so it is offered back in the link.
    expect(screen.getByTestId("de-share")).toHaveTextContent("?node=ret-a&from=2026-09-17&to=2026-09-17");
  });

  it("never names to the archive a node the radar does not publish, and leaves the link as it came", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay, { "ret-b": { name: "B" } }));
    renderAt("?node=ret-gone&from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(screen.getByText(/1 of 1 days listed/)).toBeInTheDocument());
    expect(archiveCalls()).toEqual(["/api/data/archive?date=2026%2F09%2F17&limit=500&offset=0"]);
    // The day has no file of it, which is what the page says.
    expect(screen.getByTestId("de-stat-files")).toHaveTextContent("0");
    expect(screen.getByTestId("de-share")).toHaveTextContent("?node=ret-gone&from=2026-09-17&to=2026-09-17");
  });

  it("keeps the node a link named through a filter change made before the radar answers", async () => {
    let fleet!: (nodes: unknown) => void;
    requestMock.mockImplementation((url: string) =>
      isArchive(url) ? oneFilePerDay(url) : new Promise((resolve) => (fleet = resolve)),
    );
    renderAt("?node=ret-a&from=2026-09-17&to=2026-09-17");
    fireEvent.click(await screen.findByRole("button", { name: "7d" }));
    await waitFor(() => expect(screen.getByTestId("de-share")).toHaveTextContent("from=2026-09-11"));
    expect(screen.getByTestId("de-share")).toHaveTextContent("?node=ret-a&from=2026-09-11&to=2026-09-17");

    await act(async () => fleet({ nodes: { "ret-a": { name: "A" } } }));
    await waitFor(() => expect(archiveCalls().length).toBe(7));
    expect(archiveCalls().every((u) => u.endsWith("&node_ref=ret-a"))).toBe(true);
  });

  it("writes a filter change back to the query string", async () => {
    renderAt("?from=2026-09-17&to=2026-09-17");
    await waitFor(() => expect(screen.getByRole("button", { name: "7d" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() =>
      expect(screen.getByTestId("de-share")).toHaveTextContent("from=2026-09-11"),
    );
  });

  it("copies the shareable link as the bar spells it, not as the address bar does", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderAt("?to=2026-09-17&node=ret-b&node=ret-a&from=2026-09-17");

    fireEvent.click(await screen.findByRole("button", { name: "Copy link" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        `${window.location.origin}/data?node=ret-a&node=ret-b&from=2026-09-17&to=2026-09-17`,
      ),
    );
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("says when the range reaches past what is still on disk", async () => {
    // The oldest day of the three is empty, the two after it are not, which is
    // what a retention horizon looks like from the client's side.
    requestMock.mockImplementation(
      withRegistry((url: string) =>
        dayOf(url) === "2026-09-15" ? Promise.resolve({ files: [], total: 0 }) : oneFilePerDay(url),
      ),
    );
    renderAt();
    // Scoped to the notice: the day it names is also a day header below.
    await waitFor(() =>
      expect(screen.getByText(/cold storage/i)).toHaveTextContent("2026-09-16"),
    );
  });

  it("does not claim a horizon while the range is fully populated", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay));
    renderAt();
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("3"));
    expect(screen.queryByText(/cold storage/i)).not.toBeInTheDocument();
  });

  it("keeps a ticked file in the basket after the filters stop matching it", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay));
    renderAt("?from=2026-09-10&to=2026-09-17");
    const oldest = await screen.findByRole("checkbox", { name: "Select every file on 2026-09-10" });
    await waitFor(() => expect(oldest).toBeEnabled());
    fireEvent.click(oldest);
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("1 file selected");

    // Reset narrows the range to the last three days, which no longer
    // include the file, and the basket must not notice.
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() =>
      expect(screen.queryByRole("checkbox", { name: /on 2026-09-10/ })).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("1 file selected");
    fireEvent.click(screen.getByRole("button", { name: "Show manifest" }));
    expect(screen.getByRole("textbox", { name: /manifest/i })).toHaveValue(
      `${window.location.origin}/api/data/archive/year=2026/month=09/day=10/node_ref=ret-a/part-0.parquet`,
    );
  });

  it("selects everything the filters match, and clears it again", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay));
    renderAt();
    await waitFor(() => expect(screen.getByTestId("de-stat-files")).toHaveTextContent("3"));
    fireEvent.click(screen.getByRole("button", { name: "Select all matching" }));
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("3 files selected");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("0 files selected");
  });

  it("previews a file and adds it to the basket from the drawer", async () => {
    requestMock.mockImplementation(withRegistry(oneFilePerDay));
    renderAt("?from=2026-09-17&to=2026-09-17");
    fireEvent.click(await screen.findByRole("button", { name: "Preview part-0.parquet" }));
    const dialog = screen.getByRole("dialog", { name: "part-0.parquet" });
    expect(dialog).toHaveTextContent("node_ref=ret-a");
    fireEvent.click(screen.getByRole("button", { name: "Add to basket" }));
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("1 file selected");
    // The manifest opens with the addition, so it is seen to land.
    expect(screen.getByRole<HTMLTextAreaElement>("textbox", { name: /manifest/i }).value).toMatch(
      /part-0\.parquet$/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("flips a phone preview's basket button as the file goes in and out", async () => {
    stubViewport(390);
    try {
      requestMock.mockImplementation(withRegistry(oneFilePerDay));
      renderAt("?from=2026-09-17&to=2026-09-17");
      const [card] = await screen.findAllByTestId("de-file");
      fireEvent.click(card.querySelector(".de-fname")!);
      const dialog = screen.getByRole("dialog", { name: "part-0.parquet" });

      fireEvent.click(within(dialog).getByRole("button", { name: "Add to basket" }));
      expect(screen.getByTestId("de-basket-count")).toHaveTextContent("1 file");
      fireEvent.click(within(dialog).getByRole("button", { name: "Remove from basket" }));
      // An empty basket has no bar on a phone.
      expect(screen.queryByTestId("de-basket")).not.toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Add to basket" })).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("puts every filter back to its default at once", async () => {
    renderAt("?from=2026-09-01&to=2026-09-17&minsize=4096&tod=06:00-07:00&near=51.5,-0.1,10");
    await waitFor(() => expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() =>
      expect(screen.getByTestId("de-share")).toHaveTextContent(
        "?from=2026-09-15&to=2026-09-17",
      ),
    );
  });
});
