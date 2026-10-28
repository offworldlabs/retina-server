import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import SystemMetricsPage from "../pages/admin/SystemMetricsPage";
import { stubViewport } from "./matchMedia";

vi.mock("../api/client", () => ({ api: { adminMetrics: vi.fn() } }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SystemMetricsPage", () => {
  it("heads each phone card of the task table with the task's name", async () => {
    stubViewport(390);
    vi.mocked(api.adminMetrics).mockResolvedValue({
      task_last_success: { "archive-writer": 1_700_000_000 },
      task_error_counts: { "archive-writer": 0 },
    });
    const { container } = render(<SystemMetricsPage />);
    await screen.findByText("archive-writer");

    const name = container.querySelector("tbody > tr > td:first-child");
    expect(name).toHaveTextContent("archive-writer");
    expect(name).toHaveAttribute("data-hint", "title");
  });
});
