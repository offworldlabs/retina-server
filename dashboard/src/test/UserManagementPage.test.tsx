import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import UserManagementPage from "../pages/admin/UserManagementPage";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    adminUsers: vi.fn(),
    adminNodeOwners: vi.fn(),
    adminSetRole: vi.fn(),
  },
}));

const ADA = { id: "u1", name: "Ada", email: "ada@example.com", provider: "email", role: "user" };

beforeEach(() => {
  vi.mocked(api.adminUsers).mockReset().mockResolvedValue([ADA]);
  vi.mocked(api.adminNodeOwners).mockReset().mockResolvedValue({});
  vi.mocked(api.adminSetRole).mockReset();
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UserManagementPage changing a role", () => {
  it("says so when the change fails, and keeps the role shown", async () => {
    vi.mocked(api.adminSetRole).mockRejectedValue(new Error("403 Forbidden"));
    render(<UserManagementPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Promote" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not make Ada an admin: 403 Forbidden");
    expect(screen.getByRole("button", { name: "Promote" })).toBeEnabled();
    expect(api.adminUsers).toHaveBeenCalledOnce();
  });

  it("fetches the users again once the change lands", async () => {
    vi.mocked(api.adminSetRole).mockResolvedValue({});
    render(<UserManagementPage />);
    vi.mocked(api.adminUsers).mockResolvedValue([{ ...ADA, role: "admin" }]);
    fireEvent.click(await screen.findByRole("button", { name: "Promote" }));

    expect(api.adminSetRole).toHaveBeenCalledWith("u1", "admin");
    expect(await screen.findByRole("button", { name: "Demote" })).toBeInTheDocument();
    await waitFor(() => expect(api.adminUsers).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
