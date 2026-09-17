import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DayEntry } from "../../pages/user/dataExplorer/archive";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { ResultsTree } from "../../pages/user/dataExplorer/ResultsTree";

const file = (day: string, node: string, name: string, over: Partial<ArchiveFile> = {}): ArchiveFile => ({
  key: `${day}/${node}/${name}`,
  name,
  node,
  day,
  size: 1024,
  endMs: Date.parse(`${day}T06:00:00Z`),
  startMs: Date.parse(`${day}T05:00:00Z`),
  ...over,
});

const done = (day: string): DayEntry => ({ day, nodeId: null, status: "done", files: [] });

function setup(over: Partial<Parameters<typeof ResultsTree>[0]> = {}) {
  const onRetry = vi.fn();
  const onSort = vi.fn();
  const days = over.days ?? ["2026-09-16", "2026-09-17"];
  const byDay =
    over.byDay ??
    new Map([
      ["2026-09-17", [file("2026-09-17", "ret-a", "a.parquet")]],
      ["2026-09-16", [file("2026-09-16", "ret-b", "b.parquet")]],
    ]);
  render(
    <ResultsTree
      days={days}
      byDay={byDay}
      entryFor={over.entryFor ?? ((d) => done(d))}
      sort={over.sort ?? "span"}
      onSort={onSort}
      onRetry={onRetry}
    />,
  );
  return { onRetry, onSort };
}

describe("ResultsTree", () => {
  it("puts the newest day first", () => {
    setup();
    const headings = screen.getAllByRole("button", { name: /2026-09-1/ });
    expect(headings[0]).toHaveTextContent("2026-09-17");
  });

  it("shows a file with its node, size and estimated span", () => {
    // One day, one file: the default fixture has a file per day, and both
    // render the same size and the same link text.
    setup({
      days: ["2026-09-17"],
      byDay: new Map([["2026-09-17", [file("2026-09-17", "ret-a", "a.parquet")]]]),
    });
    expect(screen.getByText("a.parquet")).toBeInTheDocument();
    expect(screen.getByText("1.0 KB")).toBeInTheDocument();
    expect(screen.getByText("05:00 → 06:00")).toBeInTheDocument();
    expect(screen.getAllByText("est.").length).toBeGreaterThan(0);
  });

  it("says a day is still listing", () => {
    setup({ entryFor: () => null });
    expect(screen.getAllByText(/Listing/).length).toBeGreaterThan(0);
  });

  it("offers a retry for a failed day and reports which one", async () => {
    const { onRetry } = setup({
      days: ["2026-09-17"],
      byDay: new Map(),
      entryFor: (day) => ({ day, nodeId: null, status: "error", files: [], error: "HTTP 503" }),
    });
    expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledWith("2026-09-17");
  });

  it("says when a listed day matches nothing", () => {
    setup({ days: ["2026-09-17"], byDay: new Map() });
    expect(screen.getByText(/No files for this day/)).toBeInTheDocument();
  });

  it("collapses and expands a day", async () => {
    setup({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", [file("2026-09-17", "ret-a", "a.parquet")]]]) });
    expect(screen.getByText("a.parquet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /2026-09-17/ }));
    expect(screen.queryByText("a.parquet")).not.toBeInTheDocument();
  });

  it("opens a day with more than three nodes collapsed", () => {
    const files = ["a", "b", "c", "d"].map((n) => file("2026-09-17", `ret-${n}`, `${n}.parquet`));
    setup({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", files]]) });
    expect(screen.queryByText("a.parquet")).not.toBeInTheDocument();
    expect(screen.getByText(/4 nodes/)).toBeInTheDocument();
  });

  it("keeps a group the reader opened open when more files arrive for the same nodes", async () => {
    const files = ["a", "b", "c", "d"].map((n) => file("2026-09-17", `ret-${n}`, `${n}.parquet`));
    const { rerender } = render(
      <ResultsTree
        days={["2026-09-17"]}
        byDay={new Map([["2026-09-17", files]])}
        entryFor={(d) => done(d)}
        sort="span"
        onSort={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    // Four nodes, so every node group starts collapsed while the day itself
    // stays open. Opening one is the reader's choice that must survive.
    fireEvent.click(screen.getByRole("button", { name: /ret-a/ }));
    expect(screen.getByText("a.parquet")).toBeInTheDocument();

    const more = [...files, file("2026-09-17", "ret-a", "a2.parquet")];
    rerender(
      <ResultsTree
        days={["2026-09-17"]}
        byDay={new Map([["2026-09-17", more]])}
        entryFor={(d) => done(d)}
        sort="span"
        onSort={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("a.parquet")).toBeInTheDocument();
  });

  it("re-applies the default when a day's set of nodes changes", () => {
    const four = ["a", "b", "c", "d"].map((n) => file("2026-09-17", `ret-${n}`, `${n}.parquet`));
    const { rerender } = render(
      <ResultsTree
        days={["2026-09-17"]}
        byDay={new Map([["2026-09-17", four]])}
        entryFor={(d) => done(d)}
        sort="span"
        onSort={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /ret-a/ }));
    expect(screen.getByText("a.parquet")).toBeInTheDocument();
    expect(screen.queryByText("b.parquet")).not.toBeInTheDocument();

    // Two nodes is under the threshold, so the whole day returns to the
    // default and opens. A collapse state belongs to a set of nodes, not to
    // a node, which is what lets a filter change re-decide the layout.
    rerender(
      <ResultsTree
        days={["2026-09-17"]}
        byDay={new Map([["2026-09-17", four.slice(0, 2)]])}
        entryFor={(d) => done(d)}
        sort="span"
        onSort={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("b.parquet")).toBeInTheDocument();
  });

  it("sorts files within a node group", () => {
    const files = [
      file("2026-09-17", "ret-a", "small.parquet", { size: 10 }),
      file("2026-09-17", "ret-a", "big.parquet", { size: 9999 }),
    ];
    setup({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", files]]), sort: "size" });
    const rows = screen.getAllByTestId("de-file");
    expect(within(rows[0]).getByText("big.parquet")).toBeInTheDocument();
  });

  it("reports a sort request", async () => {
    const { onSort } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Size" }));
    expect(onSort).toHaveBeenCalledWith("size");
  });

  it("links each file to its download", () => {
    setup({
      days: ["2026-09-17"],
      byDay: new Map([["2026-09-17", [file("2026-09-17", "ret-a", "a.parquet")]]]),
    });
    expect(screen.getByRole("link", { name: "JSON" })).toHaveAttribute(
      "href",
      "/api/data/archive/2026-09-17/ret-a/a.parquet",
    );
  });
});
