import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: {
    adminUsers: vi.fn(),
    adminDeactivateUser: vi.fn(),
    adminReactivateUser: vi.fn(),
    adminSendSignInLink: vi.fn(),
    adminDeleteUser: vi.fn(),
    adminChangeUserEmail: vi.fn(),
  },
}));

import { api } from "../api/client";
import UserManagementPage from "../pages/admin/UserManagementPage";
import type { AdminUser, AdminUserPage } from "../types";

const NOW = new Date("2026-10-15T12:00:00Z");
const HOUR = 3600;
const nowSeconds = NOW.getTime() / 1000;

const ADA: AdminUser = {
  id: "u1",
  email: "ada@example.com",
  name: "Ada",
  role: "user",
  is_superuser: false,
  is_active: true,
  created_at: nowSeconds - 30 * 86400,
  last_seen_at: null,
  is_verified: true,
  node_count: 2,
};

const BOB: AdminUser = {
  ...ADA,
  id: "u2",
  email: "bob@example.com",
  name: "",
  is_active: false,
  is_verified: false,
  node_count: 0,
  last_seen_at: nowSeconds - 3 * HOUR,
};

/** `count` made-up accounts, for a page of them. */
function accounts(count: number, from = 0): AdminUser[] {
  return Array.from({ length: count }, (_, i) => ({
    ...ADA,
    id: `p${from + i}`,
    email: `user${from + i}@example.com`,
    name: `User ${from + i}`,
  }));
}

function envelope(users: AdminUser[], total = users.length): AdminUserPage {
  return { users, total };
}

/** The address the page has written, beside the page. */
function Address() {
  const { search } = useLocation();
  return <span data-testid="search">{search}</span>;
}

function Detail() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Back
    </button>
  );
}

function renderAt(entry = "/users") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Link to="/users">All users</Link>
      <Routes>
        <Route
          path="/users"
          element={
            <>
              <UserManagementPage />
              <Address />
            </>
          }
        />
        <Route path="/users/:userId" element={<Detail />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const search = () => screen.getByTestId("search").textContent;
const searchbox = () => screen.getByRole("searchbox", { name: "Search users by email" });
const lastAsk = () => vi.mocked(api.adminUsers).mock.lastCall?.[0];
const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((cell) => cell.textContent);
const statValue = (label: string) =>
  screen.getByText(label).closest(".stat-card")?.querySelector(".stat-value")?.textContent;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.resetAllMocks();
  vi.mocked(api.adminUsers).mockResolvedValue(envelope([ADA, BOB]));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("UserManagementPage", () => {
  it("lists each account from the envelope with its state, and the total", async () => {
    renderAt();
    await flush();

    expect(api.adminUsers).toHaveBeenCalledWith({ q: "", limit: 25, offset: 0 });
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "User",
      "Nodes",
      "Last visit",
      "Verified",
      "State",
      "Actions",
    ]);

    // The name is the link, the address under it.
    const ada = screen.getByRole("link", { name: "Ada" });
    expect(ada).toHaveAttribute("href", "/users/u1");
    const adaRow = ada.closest("tr") as HTMLElement;
    const adaUser = within(adaRow).getAllByRole("cell")[0];
    expect(within(adaUser).getByText("ada@example.com")).not.toBe(ada);
    expect(adaUser).toHaveTextContent(/^Adaada@example\.com$/);
    expect(cells(adaRow).slice(1, 5)).toEqual(["2", "Never", "Verified", "Active"]);

    // No name yet: the address is the link, and is not said twice.
    const bob = screen.getByRole("link", { name: "bob@example.com" });
    const bobRow = bob.closest("tr") as HTMLElement;
    expect(within(bobRow).getAllByText("bob@example.com")).toEqual([bob]);
    expect(cells(bobRow).slice(0, 5)).toEqual(["bob@example.com", "0", "3h ago", "Unverified", "Deactivated"]);
    expect(within(bobRow).getByText("Unverified")).toHaveClass("badge", "warning");
    expect(within(bobRow).getByText("Deactivated")).toHaveClass("badge", "offline");
    expect(within(bobRow).getByText("3h ago")).toHaveAttribute("title", new Date(BOB.last_seen_at! * 1000).toLocaleString());
    expect(within(bobRow).getByRole("group", { name: "Actions for bob@example.com" })).toBeInTheDocument();
    expect(within(bobRow).getByRole("button", { name: "Reactivate" })).toBeInTheDocument();

    expect(statValue("Total Users")).toBe("2");
  });

  it("takes a name of spaces for none", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope([{ ...ADA, name: "   " }]));
    renderAt();
    await flush();

    const link = screen.getByRole("link", { name: "ada@example.com" });
    expect(within(link.closest("tr") as HTMLElement).getAllByText("ada@example.com")).toEqual([link]);
  });

  it("says whose accounts it lists, and whose it does not", async () => {
    renderAt();
    await flush();

    expect(screen.getByRole("heading", { level: 1, name: "User Management" }).nextElementSibling).toHaveTextContent(
      "Accounts opened by an emailed sign-in or claim link, or made for a polled radar. Administrators come from Cloudflare Access and are not listed here.",
    );
  });

  it("searches the server once typing pauses, from the first page", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(25), 80));
    renderAt("/users?page=3");
    await flush();
    expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 50 });

    fireEvent.change(searchbox(), { target: { value: "a" } });
    await flush(100);
    fireEvent.change(searchbox(), { target: { value: "ad" } });
    await flush(100);
    fireEvent.change(searchbox(), { target: { value: " ada " } });
    await flush(299);
    expect(api.adminUsers).toHaveBeenCalledTimes(1);

    vi.mocked(api.adminUsers).mockResolvedValue(envelope([ADA]));
    await flush(1);

    expect(api.adminUsers).toHaveBeenCalledTimes(2);
    expect(lastAsk()).toEqual({ q: "ada", limit: 25, offset: 0 });
    expect(search()).toBe("?q=ada");
    expect(statValue("Matching Users")).toBe("1");
  });

  it("asks for nothing new when only spaces change around the search", async () => {
    renderAt("/users?q=ada");
    await flush();

    fireEvent.change(searchbox(), { target: { value: "ada  " } });
    await flush(400);

    expect(api.adminUsers).toHaveBeenCalledTimes(1);
  });

  it("turns pages by offset, keeping the rows on screen until the next page lands", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(25), 60));
    renderAt();
    await flush();
    expect(screen.getByText(/Page 1 of 3/)).toHaveTextContent("Page 1 of 3 (60 users)");
    expect(screen.getByText("Showing 1–25 of 60")).toBeInTheDocument();

    let land: (page: AdminUserPage) => void = () => {};
    vi.mocked(api.adminUsers).mockReturnValue(
      new Promise((resolve) => {
        land = resolve;
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    await flush();

    expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 25 });
    expect(search()).toBe("?page=2");
    // No flash: the first page stays while the second is on its way.
    expect(screen.getByRole("link", { name: "User 0" })).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).toBeNull();

    await act(async () => {
      land(envelope(accounts(25, 25), 60));
    });
    expect(screen.queryByRole("link", { name: "User 0" })).toBeNull();
    expect(screen.getByRole("link", { name: "User 25" })).toBeInTheDocument();
    expect(screen.getByText("Showing 26–50 of 60")).toBeInTheDocument();

    vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(10, 50), 60));
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    await flush();
    expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 50 });
    expect(search()).toBe("?page=3");

    fireEvent.click(screen.getByRole("button", { name: /Prev/ }));
    await flush();
    expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 25 });
    expect(search()).toBe("?page=2");
  });

  it("reads the search and the page from the address", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(5, 25), 30));
    renderAt("/users?q=user&page=2");
    await flush();

    expect(api.adminUsers).toHaveBeenCalledWith({ q: "user", limit: 25, offset: 25 });
    expect(searchbox()).toHaveValue("user");
    expect(screen.getByText(/Page 2 of 2/)).toBeInTheDocument();
  });

  it.each(["?page=0", "?page=-2", "?page=two", "?page=1.5"])("reads %s as the first page", async (query) => {
    renderAt(`/users${query}`);
    await flush();
    expect(api.adminUsers).toHaveBeenCalledWith({ q: "", limit: 25, offset: 0 });
  });

  it("comes back from an account's page to the same search and page", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(5, 25), 30));
    renderAt("/users?q=user&page=2");
    await flush();

    fireEvent.click(screen.getByRole("link", { name: "User 25" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await flush();

    expect(searchbox()).toHaveValue("user");
    expect(lastAsk()).toEqual({ q: "user", limit: 25, offset: 25 });
    expect(search()).toBe("?q=user&page=2");
  });

  it("follows the address when it changes under the search box", async () => {
    renderAt("/users?q=ada");
    await flush();

    fireEvent.click(screen.getByRole("link", { name: "All users" }));
    await flush(400);

    expect(searchbox()).toHaveValue("");
    expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 0 });
    expect(search()).toBe("");
  });

  it("turns back to the last page when the one asked for is past the end", async () => {
    vi.mocked(api.adminUsers).mockImplementation(async ({ offset = 0 } = {}) =>
      envelope(offset >= 30 ? [] : accounts(5, 25), 30),
    );
    renderAt("/users?page=9");
    await flush();
    await flush();

    expect(search()).toBe("?page=2");
    expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 25 });
    expect(screen.getByRole("link", { name: "User 25" })).toBeInTheDocument();
  });

  it("names the search when nothing matches", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope([]));
    renderAt("/users?q=zed");
    await flush();

    expect(screen.getByText("No users match “zed”")).toBeInTheDocument();
    expect(statValue("Matching Users")).toBe("0");
  });

  it("says so when there are no accounts at all", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue(envelope([]));
    renderAt();
    await flush();

    expect(screen.getByText("No users registered yet")).toBeInTheDocument();
  });

  it("says why the list could not load, with a retry", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(api.adminUsers).mockRejectedValue(new Error("Network down"));
    renderAt();
    await flush();

    expect(screen.getByRole("alert")).toHaveTextContent("Could not load the user list: Network down");
    expect(screen.queryByRole("table")).toBeNull();

    vi.mocked(api.adminUsers).mockResolvedValue(envelope([ADA]));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await flush();
    expect(screen.getByRole("link", { name: "Ada" })).toBeInTheDocument();
  });

  it("refetches after an action, and says what a delete took with it", async () => {
    vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 2 });
    renderAt();
    await flush();
    const row = screen.getByRole("link", { name: "Ada" }).closest("tr") as HTMLElement;

    vi.mocked(api.adminUsers).mockResolvedValue(envelope([BOB]));
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this account?" });
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
    await flush();

    expect(api.adminDeleteUser).toHaveBeenCalledWith("u1");
    expect(api.adminUsers).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("link", { name: "Ada" })).toBeNull();
    const notice = screen.getByText("Deleted ada@example.com. 2 nodes released.");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveFocus();
  });

  describe("what a delete leaves said", () => {
    /** Deletes the account in `row` through its dialog, and lets the refetch land. */
    async function remove(email: string) {
      const row = screen.getByRole("group", { name: `Actions for ${email}` }).closest("tr") as HTMLElement;
      fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
      const dialog = screen.getByRole("dialog", { name: "Delete this account?" });
      fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: email } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
      await flush();
    }

    it("goes with the next search", async () => {
      vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 0 });
      renderAt();
      await flush();
      vi.mocked(api.adminUsers).mockResolvedValue(envelope([BOB]));
      await remove("ada@example.com");
      expect(screen.getByText("Deleted ada@example.com.")).toBeInTheDocument();

      fireEvent.change(searchbox(), { target: { value: "bob" } });
      await flush(300);

      expect(lastAsk()).toEqual({ q: "bob", limit: 25, offset: 0 });
      expect(screen.queryByText("Deleted ada@example.com.")).toBeNull();
    });

    it("goes with the next page", async () => {
      vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 0 });
      vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(25), 60));
      renderAt();
      await flush();
      vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(25, 1), 59));
      await remove("user0@example.com");
      expect(screen.getByText("Deleted user0@example.com.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /Next/ }));
      await flush();

      expect(search()).toBe("?page=2");
      expect(screen.queryByText("Deleted user0@example.com.")).toBeNull();
    });

    it("goes with the next action on a row, before its answer", async () => {
      vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 2 });
      renderAt();
      await flush();
      vi.mocked(api.adminUsers).mockResolvedValue(envelope([BOB]));
      await remove("ada@example.com");
      expect(screen.getByText("Deleted ada@example.com. 2 nodes released.")).toBeInTheDocument();

      vi.mocked(api.adminSendSignInLink).mockReturnValue(new Promise(() => {}));
      fireEvent.click(screen.getByRole("button", { name: "Send sign-in link" }));

      expect(screen.queryByText(/^Deleted /)).toBeNull();
    });

    it("stays when the delete empties the last page and the list turns back", async () => {
      vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 0 });
      vi.mocked(api.adminUsers).mockResolvedValue(envelope(accounts(1, 25), 26));
      renderAt("/users?page=2");
      await flush();

      vi.mocked(api.adminUsers).mockImplementation(async ({ offset = 0 } = {}) =>
        envelope(offset >= 25 ? [] : accounts(25), 25),
      );
      await remove("user25@example.com");
      await flush();

      expect(search()).toBe("");
      expect(lastAsk()).toEqual({ q: "", limit: 25, offset: 0 });
      const notice = screen.getByText("Deleted user25@example.com.");
      expect(notice).toHaveFocus();
    });
  });
});
