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

const ADA = { id: "u1", name: "Ada", email: "ada@example.com", provider: "email", role: "user" };

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
    expect(cells).toEqual(["Ada", "ada@example.com", "email", "1", "—"]);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
