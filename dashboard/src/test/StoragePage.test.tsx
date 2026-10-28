import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: { adminStorage: vi.fn(), archive: vi.fn(), adminNodeRefs: vi.fn() } }));

import { api } from "../api/client";
import StoragePage from "../pages/admin/StoragePage";
import { stubViewport } from "./matchMedia";

const file = (i: number) => ({ key: `archive/2026/09/ret-a/f${i}.json`, size_bytes: 1024, modified: null });
const listing = (total: number, offset: number) => ({
  files: Array.from({ length: Math.max(0, Math.min(50, total - offset)) }, (_, i) => file(offset + i)),
  total,
});

describe("StoragePage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.adminStorage).mockResolvedValue({
      archive_files: 120, archive_mb: 1, archive_bytes: 1048576,
      disk: { total_gb: 100, used_gb: 80, free_gb: 20, used_pct: 80 },
    });
    vi.mocked(api.adminNodeRefs).mockResolvedValue({});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("names a listed file's node by the id the per-node table uses", async () => {
    vi.mocked(api.adminNodeRefs).mockResolvedValue({ nde0123456789: "ret1a2b3c4d" });
    vi.mocked(api.archive).mockResolvedValue({
      files: [{ key: "archive/year=2026/month=09/day=24/node_ref=nde0123456789/part-0.parquet", size_bytes: 1, modified: null }],
      total: 1,
    });
    render(<StoragePage />);
    expect(await screen.findByText("ret1a2b3c4d")).toBeInTheDocument();
    expect(screen.queryByText(/node_ref=/)).not.toBeInTheDocument();
  });

  it("pages the archive listing with the shared pager", async () => {
    vi.mocked(api.archive).mockImplementation(async (_limit, offset) => listing(120, offset));
    render(<StoragePage />);
    expect(await screen.findByText("Page 1 of 3 (120 archives)")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Next →"));
    expect(await screen.findByText("Page 2 of 3 (120 archives)")).toBeInTheDocument();
    expect(api.archive).toHaveBeenLastCalledWith(50, 50);
  });

  it("falls back to the last page when the archive shrinks under the pager", async () => {
    let shrunk = false;
    vi.mocked(api.archive).mockImplementation(async (_limit, offset) => {
      // The third page is the first request to see the archive pruned to 60.
      if (offset === 100) shrunk = true;
      return listing(shrunk ? 60 : 120, offset);
    });
    render(<StoragePage />);
    await screen.findByText("Page 1 of 3 (120 archives)");
    fireEvent.click(screen.getByText("Next →"));
    await screen.findByText("Page 2 of 3 (120 archives)");
    fireEvent.click(screen.getByText("Next →"));
    expect(await screen.findByText("Page 2 of 2 (60 archives)")).toBeInTheDocument();
    await waitFor(() => expect(api.archive).toHaveBeenLastCalledWith(50, 50));
    expect(screen.getByText("f50.json")).toBeInTheDocument();
  });

  it("draws the disk as a usage bar", async () => {
    vi.mocked(api.archive).mockResolvedValue(listing(0, 0));
    render(<StoragePage />);
    const bar = await screen.findByRole("progressbar", { name: "80.0 GB used of 100.0 GB" });
    expect(bar).toHaveAttribute("aria-valuenow", "80");
    expect(bar.querySelector(".usage-bar-fill")).toHaveClass("warning");
  });

  describe("archive names", () => {
    const listed = (...names: string[]) =>
      vi.mocked(api.archive).mockResolvedValue({
        files: names.map((name) => ({ key: `archive/year=2026/month=09/day=24/node_ref=nde0123456789/${name}`, size_bytes: 1, modified: null })),
        total: names.length,
      });
    const nameCells = (container: HTMLElement) =>
      Array.from(container.querySelectorAll(".table-wrapper tbody > tr > td:first-child:not(.table-note)"));

    it("keep the end, extension included, in view on a phone", async () => {
      stubViewport(390);
      listed(
        "part-133334-29b7c2de562f4b548b33c8a2af9ba145.parquet",
        "checkpoint012345",
        "a.json",
        "a.very-long-descriptive-suffix.json",
      );
      const { container } = render(<StoragePage />);
      await waitFor(() => expect(container.querySelectorAll(".archive-name")).toHaveLength(4));

      const [long, sixteen, short, earlyDot] = nameCells(container);
      const cut = (cell: Element) => [
        cell.querySelector(".archive-name")?.getAttribute("title"),
        cell.querySelector(".archive-name-head")?.textContent,
        cell.querySelector(".archive-name-tail")?.textContent,
      ];
      expect(cut(long)).toEqual([
        "part-133334-29b7c2de562f4b548b33c8a2af9ba145.parquet",
        "part-133334-29b7c2de562f4b548b33c8a2",
        "af9ba145.parquet",
      ]);
      expect(cut(sixteen)).toEqual(["checkpoint012345", "", "checkpoint012345"]);
      expect(cut(short)).toEqual(["a.json", "", "a.json"]);
      expect(cut(earlyDot)).toEqual([
        "a.very-long-descriptive-suffix.json",
        "a.very-long-descrip",
        "tive-suffix.json",
      ]);
      expect(long).toHaveAttribute("data-hint", "title");
    });

    it("show whole as one text node on a desktop", async () => {
      stubViewport(1440);
      const names = [
        "part-133334-29b7c2de562f4b548b33c8a2af9ba145.parquet",
        "a.json",
        "a.very-long-descriptive-suffix.json",
      ];
      listed(...names);
      const { container } = render(<StoragePage />);
      await screen.findByText(names[0]);

      const cells = nameCells(container);
      expect(cells.map((cell) => cell.innerHTML)).toEqual([
        "part-133334-29b7c2de562f4b548b33c8a2af9ba145.parquet",
        "a.json",
        "a.very-long-descriptive-suffix.json",
      ]);
      expect(cells[0].childNodes).toHaveLength(1);
      expect(cells[0].firstChild?.nodeType).toBe(Node.TEXT_NODE);
    });
  });
});
