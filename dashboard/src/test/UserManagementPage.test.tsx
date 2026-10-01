import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import UserManagementPage from "../pages/admin/UserManagementPage";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    adminUsers: vi.fn(),
    adminNodeOwners: vi.fn(),
  },
}));

const ADA = { id: "u1", name: "Ada", email: "ada@example.com", role: "user", last_seen_at: null };

beforeEach(() => {
  vi.mocked(api.adminUsers).mockReset().mockResolvedValue([ADA]);
  vi.mocked(api.adminNodeOwners).mockReset().mockResolvedValue({ n1: { user_id: "u1" } });
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UserManagementPage", () => {
  it("shows each account and its nodes, with no role to see or change", async () => {
    render(<UserManagementPage />);

    const row = (await screen.findByText("ada@example.com")).closest("tr") as HTMLElement;
    const cells = within(row).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual(["Ada", "ada@example.com", "1", "—"]);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("shows when an account was last seen, from the epoch seconds the API sends", async () => {
    const seen = 1790596800;
    vi.mocked(api.adminUsers).mockResolvedValue([{ ...ADA, last_seen_at: seen }]);
    render(<UserManagementPage />);

    const row = (await screen.findByText("ada@example.com")).closest("tr") as HTMLElement;
    const cells = within(row).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual(["Ada", "ada@example.com", "1", new Date(seen * 1000).toLocaleString()]);
  });
});
