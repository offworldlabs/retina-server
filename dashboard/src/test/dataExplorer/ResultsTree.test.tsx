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

type Props = Parameters<typeof ResultsTree>[0];

/** Every prop with a default, so a test names only what it is about. */
function props(over: Partial<Props> = {}): Props {
  return {
    days: ["2026-09-16", "2026-09-17"],
    byDay: new Map([
      ["2026-09-17", [file("2026-09-17", "ret-a", "a.parquet")]],
      ["2026-09-16", [file("2026-09-16", "ret-b", "b.parquet")]],
    ]),
    entryFor: (d) => done(d),
    sort: "span",
    onSort: vi.fn(),
    onRetry: vi.fn(),
    selected: new Set(),
    onSelect: vi.fn(),
    active: null,
    onPreview: vi.fn(),
    ...over,
  };
}

function setup(over: Partial<Props> = {}) {
  const p = props(over);
  const view = render(<ResultsTree {...p} />);
  return { ...p, view };
}

const ONE_DAY = {
  days: ["2026-09-17"],
  byDay: new Map([["2026-09-17", [file("2026-09-17", "ret-a", "a.parquet")]]]),
};

describe("ResultsTree", () => {
  it("puts the newest day first", () => {
    setup();
    const headings = screen.getAllByRole("button", { name: /2026-09-1/ });
    expect(headings[0]).toHaveTextContent("2026-09-17");
  });

  it("shows a file with its node, size and estimated span", () => {
    // One day, one file: the default fixture has a file per day, and both
    // render the same size and the same link text.
    setup(ONE_DAY);
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
    setup(ONE_DAY);
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
    const p = props({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", files]]) });
    const { rerender } = render(<ResultsTree {...p} />);
    // Four nodes, so every node group starts collapsed while the day itself
    // stays open. Opening one is the reader's choice that must survive.
    fireEvent.click(screen.getByRole("button", { name: /ret-a/ }));
    expect(screen.getByText("a.parquet")).toBeInTheDocument();

    const more = [...files, file("2026-09-17", "ret-a", "a2.parquet")];
    rerender(<ResultsTree {...p} byDay={new Map([["2026-09-17", more]])} />);
    expect(screen.getByText("a.parquet")).toBeInTheDocument();
  });

  it("re-applies the default when a day's set of nodes changes", () => {
    const four = ["a", "b", "c", "d"].map((n) => file("2026-09-17", `ret-${n}`, `${n}.parquet`));
    const p = props({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", four]]) });
    const { rerender } = render(<ResultsTree {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /ret-a/ }));
    expect(screen.getByText("a.parquet")).toBeInTheDocument();
    expect(screen.queryByText("b.parquet")).not.toBeInTheDocument();

    // Two nodes is under the threshold, so the whole day returns to the
    // default and opens. A collapse state belongs to a set of nodes, not to
    // a node, which is what lets a filter change re-decide the layout.
    rerender(<ResultsTree {...p} byDay={new Map([["2026-09-17", four.slice(0, 2)]])} />);
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
    setup(ONE_DAY);
    expect(screen.getByRole("link", { name: "JSON" })).toHaveAttribute(
      "href",
      "/api/data/archive/2026-09-17/ret-a/a.parquet",
    );
  });

  it("summarises what the filters matched", () => {
    setup();
    expect(screen.getByText("2 files · 2.0 KB")).toBeInTheDocument();
  });

  it("opens every day at once", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    for (const day of ["2026-09-16", "2026-09-17"]) {
      expect(screen.getByRole("button", { name: new RegExp(day) })).toHaveAttribute(
        "aria-expanded",
        "true",
      );
    }
  });

  it("closes every day at once, and the files with them", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(screen.getByRole("button", { name: /2026-09-17/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("a.parquet")).not.toBeInTheDocument();
  });

  describe("selection", () => {
    it("ticks a file into the basket, and out again", () => {
      const { onSelect } = setup({ selected: new Set(["2026-09-16/ret-b/b.parquet"]) });
      fireEvent.click(screen.getByRole("checkbox", { name: "Select a.parquet" }));
      expect(onSelect).toHaveBeenLastCalledWith(["2026-09-17/ret-a/a.parquet"], true);
      fireEvent.click(screen.getByRole("checkbox", { name: "Select b.parquet" }));
      expect(onSelect).toHaveBeenLastCalledWith(["2026-09-16/ret-b/b.parquet"], false);
    });

    it("ticks a whole node group, and a whole day, without toggling them", () => {
      const files = [
        file("2026-09-17", "ret-a", "a1.parquet"),
        file("2026-09-17", "ret-a", "a2.parquet"),
        file("2026-09-17", "ret-b", "b1.parquet"),
      ];
      const { onSelect } = setup({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", files]]) });
      fireEvent.click(
        screen.getByRole("checkbox", { name: "Select every file from ret-a on 2026-09-17" }),
      );
      expect(onSelect).toHaveBeenLastCalledWith(
        ["2026-09-17/ret-a/a1.parquet", "2026-09-17/ret-a/a2.parquet"],
        true,
      );
      fireEvent.click(screen.getByRole("checkbox", { name: "Select every file on 2026-09-17" }));
      expect(onSelect).toHaveBeenLastCalledWith(files.map((f) => f.key), true);
      // The tick landed on the checkbox, not on the head around it.
      expect(screen.getByText("a1.parquet")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /2026-09-17/ })).toHaveAttribute(
        "aria-expanded",
        "true",
      );
    });

    it("shows a group ticked only once every file in it is", () => {
      const files = [file("2026-09-17", "ret-a", "a1.parquet"), file("2026-09-17", "ret-a", "a2.parquet")];
      const p = props({ days: ["2026-09-17"], byDay: new Map([["2026-09-17", files]]) });
      const { rerender } = render(<ResultsTree {...p} selected={new Set([files[0].key])} />);
      const group = () => screen.getByRole("checkbox", { name: /every file from ret-a/ });
      expect(group()).not.toBeChecked();
      rerender(<ResultsTree {...p} selected={new Set(files.map((f) => f.key))} />);
      expect(group()).toBeChecked();
      expect(screen.getByRole("checkbox", { name: /every file on 2026-09-17/ })).toBeChecked();
    });

    it("has nothing to tick for a day with no matches", () => {
      setup({ days: ["2026-09-17"], byDay: new Map() });
      expect(screen.getByRole("checkbox", { name: /every file on 2026-09-17/ })).toBeDisabled();
    });

    it("selects everything the filters match at once", () => {
      const { onSelect } = setup();
      fireEvent.click(screen.getByRole("button", { name: "Select all matching" }));
      expect(onSelect).toHaveBeenCalledWith(
        ["2026-09-17/ret-a/a.parquet", "2026-09-16/ret-b/b.parquet"],
        true,
      );
    });

    it("has nothing to select all of before anything matches", () => {
      setup({ byDay: new Map() });
      expect(screen.getByRole("button", { name: "Select all matching" })).toBeDisabled();
    });
  });

  describe("preview", () => {
    it("opens from the button and from the row, but not from the checkbox or the link", () => {
      const { onPreview } = setup(ONE_DAY);
      fireEvent.click(screen.getByRole("button", { name: "Preview a.parquet" }));
      expect(onPreview).toHaveBeenCalledTimes(1);
      expect(onPreview).toHaveBeenCalledWith("2026-09-17/ret-a/a.parquet");
      fireEvent.click(screen.getByText("a.parquet"));
      expect(onPreview).toHaveBeenCalledTimes(2);
      fireEvent.click(screen.getByRole("checkbox", { name: "Select a.parquet" }));
      fireEvent.click(screen.getByRole("link", { name: "JSON" }));
      expect(onPreview).toHaveBeenCalledTimes(2);
    });

    it("marks the file that is open", () => {
      setup({ ...ONE_DAY, active: "2026-09-17/ret-a/a.parquet" });
      expect(screen.getByTestId("de-file")).toHaveClass("active");
    });
  });
});
