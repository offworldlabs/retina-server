import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: { adminStorage: vi.fn(), archive: vi.fn() } }));

import { api } from "../api/client";
import StoragePage from "../pages/admin/StoragePage";

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
  });

  it("draws the disk as a usage bar", async () => {
    vi.mocked(api.archive).mockResolvedValue(listing(0, 0));
    render(<StoragePage />);
    const bar = await screen.findByRole("progressbar", { name: "80.0 GB used of 100.0 GB" });
    expect(bar).toHaveAttribute("aria-valuenow", "80");
    expect(bar.querySelector(".usage-bar-fill")).toHaveClass("warning");
  });
});
