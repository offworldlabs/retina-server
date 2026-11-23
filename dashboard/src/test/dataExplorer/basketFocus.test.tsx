import { createRef } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";
import { DownloadBasket } from "../../pages/user/dataExplorer/DownloadBasket";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { stubViewport } from "../matchMedia";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));
vi.mock("../../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

/** One file on the day asked for, and an empty node registry. */
function oneFile(url: string) {
  if (!url.startsWith("/api/data/archive")) return Promise.resolve({ nodes: {} });
  const day = new URL(url, "http://localhost").searchParams.get("date")!.replace(/\//g, "-");
  const [y, m, d] = day.split("-");
  const key = `year=${y}/month=${m}/day=${d}/node_ref=ret-a/part-0.parquet`;
  return Promise.resolve({ files: [{ key, size_bytes: 1024, modified: `${day}T06:00:00Z` }], total: 1 });
}

/** The explorer on one day with one file, once that file's checkbox is there. */
async function renderExplorer() {
  render(
    <MemoryRouter initialEntries={["/data?from=2026-09-17&to=2026-09-17"]}>
      <DataExplorerPage />
    </MemoryRouter>,
  );
  const box = await screen.findByRole("checkbox", { name: "Select part-0.parquet" });
  await waitFor(() => expect(box).toBeEnabled());
  return box;
}

/** Focuses a control and clicks it, as a key press or a tap that focuses does. */
function press(control: HTMLElement) {
  control.focus();
  fireEvent.click(control);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-17T12:00:00Z"));
  requestMock.mockReset();
  requestMock.mockImplementation(oneFile);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Data Explorer basket focus on a phone", () => {
  it("brings focus to the results' heading when Clear in the sheet empties the basket", async () => {
    stubViewport(390);
    const box = await renderExplorer();
    fireEvent.click(box);
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    const sheet = screen.getByRole("dialog", { name: "Download basket" });

    press(within(sheet).getByRole("button", { name: "Clear" }));

    expect(screen.queryByTestId("de-basket")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Archived detections" }));
  });

  it("leaves focus on the checkbox when unticking the last file empties the basket", async () => {
    stubViewport(390);
    const box = await renderExplorer();
    press(box);
    expect(screen.getByTestId("de-basket")).toBeInTheDocument();

    press(box);

    expect(screen.queryByTestId("de-basket")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(box);
  });

  it("brings focus to the given target when the basket empties under its bar", () => {
    stubViewport(390);
    const heading = createRef<HTMLHeadingElement>();
    const file: ArchiveFile = {
      key: "year=2026/month=09/day=17/node_ref=ret-a/a.parquet",
      name: "a.parquet",
      node: "ret-a",
      day: "2026-09-17",
      size: 1024,
      startMs: Date.parse("2026-09-17T05:00:00Z"),
      endMs: Date.parse("2026-09-17T06:00:00Z"),
      firstFrameMs: null, publishedLat: null, publishedLon: null,
    };
    const page = (files: ArchiveFile[]) => (
      <>
        <h3 ref={heading} tabIndex={-1}>Archived detections</h3>
        <DownloadBasket
          files={files}
          manifestOpen={false}
          onToggleManifest={() => {}}
          onClear={() => {}}
          focusWhenEmptied={heading}
        />
      </>
    );
    const view = render(page([file]));
    screen.getByRole("button", { name: "Download" }).focus();

    view.rerender(page([]));

    expect(document.activeElement).toBe(heading.current);
  });
});

describe("Data Explorer basket focus on a desktop", () => {
  it("keeps focus on Clear in the bar, which stays", async () => {
    stubViewport(1440);
    const box = await renderExplorer();
    fireEvent.click(box);
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("1 file selected");
    const clear = screen.getByRole("button", { name: "Clear" });

    press(clear);

    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("0 files selected");
    expect(document.activeElement).toBe(clear);
    expect(screen.getByRole("heading", { name: "Archived detections" })).not.toHaveAttribute("tabindex");
  });
});
