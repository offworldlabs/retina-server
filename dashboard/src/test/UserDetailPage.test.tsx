import { Suspense } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: {
    adminUser: vi.fn(),
    adminEvents: vi.fn(),
    adminUsers: vi.fn(),
    adminDeactivateUser: vi.fn(),
    adminReactivateUser: vi.fn(),
    adminSendSignInLink: vi.fn(),
    adminDeleteUser: vi.fn(),
    adminChangeUserEmail: vi.fn(),
  },
}));

import { api } from "../api/client";
import UserDetailPage from "../pages/admin/UserDetailPage";
import { consoleRoutes, pageTitle } from "../routes";
import type { AdminClaim, AdminEvent, AdminUser, AdminUserDetail } from "../types";

const NOW = new Date("2026-10-15T12:00:00Z");
const nowSeconds = NOW.getTime() / 1000;
const HOUR = 3600;

const ADA: AdminUser = {
  id: "u1",
  email: "ada@example.com",
  name: "Ada",
  role: "user",
  is_superuser: false,
  is_active: true,
  created_at: nowSeconds - 30 * 86400,
  last_seen_at: nowSeconds - 3 * HOUR,
  is_verified: true,
  node_count: 2,
};

const claim = (over: Partial<AdminClaim>): AdminClaim => ({
  node_id: "node-1",
  node_ref: null,
  status: "owned",
  owner: { user_id: "u1", email: "ada@example.com", name: "Ada" },
  email: "ada@example.com",
  verified: true,
  undeliverable: false,
  updated_at: nowSeconds - 2 * 86400,
  owned_since: nowSeconds - 5 * 86400,
  challenge: null,
  ...over,
});

const OWNED_WITH_REF = claim({ node_id: "node-1", node_ref: "ret-0042" });
const OWNED_NO_REF = claim({ node_id: "node-without-handle", node_ref: null, verified: false });

const PENDING = claim({
  node_id: "node-3",
  node_ref: "ret-0099",
  status: "pending",
  owner: null,
  verified: false,
  owned_since: null,
  challenge: { email: "ada@example.com", created_at: nowSeconds - 2 * HOUR, expires_at: nowSeconds + 22 * HOUR, expired: false },
});

const BOUNCED = claim({
  node_id: "node-4",
  node_ref: null,
  status: "unclaimed",
  owner: null,
  verified: false,
  undeliverable: true,
  owned_since: null,
  challenge: { email: "ada@example.com", created_at: nowSeconds - 3 * 86400, expires_at: nowSeconds - 2 * 86400, expired: true },
});

function detail(over: Partial<AdminUserDetail> = {}): AdminUserDetail {
  return { user: ADA, nodes: [OWNED_WITH_REF, OWNED_NO_REF], claims: [PENDING, BOUNCED], ...over };
}

const events = (count: number, from = 0): AdminEvent[] =>
  Array.from({ length: count }, (_, i) => ({
    ts: nowSeconds - (from + i) * 60,
    category: "user",
    message: `Event ${from + i}`,
    severity: "info",
  }));

function Address() {
  const { pathname, search } = useLocation();
  return <span data-testid="address">{`${pathname}${search}`}</span>;
}

function renderAt(entry: string | { pathname: string; state: unknown } = "/users/u1") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/users" element={<><span>The list</span><Address /></>} />
        <Route path="/users/:userId" element={<UserDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((cell) => cell.textContent);
const card = (title: string) => screen.getByRole("heading", { name: title }).closest(".card") as HTMLElement;
const statValue = (label: string) =>
  screen.getByText(label, { selector: ".stat-label" }).closest(".stat-card")?.querySelector(".stat-value")?.textContent;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  vi.resetAllMocks();
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.mocked(api.adminUser).mockResolvedValue(detail());
  vi.mocked(api.adminEvents).mockResolvedValue(events(2));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("UserDetailPage", () => {
  it("heads the page with the name, the address, their state and the facts", async () => {
    renderAt();

    expect(await screen.findByRole("heading", { level: 1, name: "Ada" })).toBeInTheDocument();
    expect(api.adminUser).toHaveBeenCalledWith("u1");
    expect(screen.getByText("ada@example.com", { selector: ".user-detail-email" })).toBeInTheDocument();
    expect(screen.getByText("Active")).toHaveClass("badge", "online");
    expect(screen.getByText("Verified", { selector: ".user-detail-badges .badge" })).toHaveClass("badge", "online");
    expect(statValue("Account created")).toContain("30d ago");
    expect(statValue("Last visit")).toBe("3h ago");
    expect(statValue("Nodes")).toBe("2");
    expect(screen.getByRole("group", { name: "Actions for ada@example.com" })).toBeInTheDocument();
  });

  it("names an account with no name by its address, and says when it is deactivated or unverified", async () => {
    vi.mocked(api.adminUser).mockResolvedValue(
      detail({ user: { ...ADA, name: "", is_active: false, is_verified: false, last_seen_at: null } }),
    );
    renderAt();

    expect(await screen.findByRole("heading", { level: 1, name: "ada@example.com" })).toBeInTheDocument();
    expect(screen.getByText("Deactivated")).toHaveClass("badge", "offline");
    expect(screen.getByText("Unverified", { selector: ".user-detail-badges .badge" })).toHaveClass("badge", "warning");
    expect(statValue("Last visit")).toBe("Never");
    expect(screen.getByRole("button", { name: "Reactivate" })).toBeInTheDocument();
  });

  it("lists the nodes it owns, linked by ref and set in mono by id when there is none", async () => {
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    const nodes = card("Nodes");
    expect(within(nodes).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Node",
      "Claim",
      "Address",
      "Verified",
      "Owned since",
    ]);
    const link = within(nodes).getByRole("link", { name: "ret-0042" });
    expect(link).toHaveAttribute("href", "/nodes/ret-0042");
    expect(cells(link.closest("tr") as HTMLElement)).toEqual(["ret-0042", "Owned", "ada@example.com", "Verified", "5d ago"]);

    const bare = within(nodes).getByText("node-without-handle");
    expect(bare).toHaveClass("mono");
    expect(within(nodes).queryByRole("link", { name: "node-without-handle" })).toBeNull();
    expect(within(bare.closest("tr") as HTMLElement).getByText("Unverified")).toHaveClass("badge", "warning");
  });

  it("reads a dash, not Unverified, for an owner assigned by an administrator, who has no claim address", async () => {
    const assigned = claim({ node_id: "node-assigned", node_ref: "ret-0077", email: null, verified: false });
    vi.mocked(api.adminUser).mockResolvedValue(detail({ nodes: [OWNED_WITH_REF, assigned] }));
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    const row = within(card("Nodes")).getByRole("link", { name: "ret-0077" }).closest("tr") as HTMLElement;
    expect(cells(row)).toEqual(["ret-0077", "Owned", "—", "—", "5d ago"]);
    expect(within(row).queryByText("Unverified")).toBeNull();
    expect(within(row).queryByText("Verified")).toBeNull();
    // An address on file still reads either way.
    const filed = within(card("Nodes")).getByRole("link", { name: "ret-0042" }).closest("tr") as HTMLElement;
    expect(within(filed).getByText("Verified")).toHaveClass("badge", "online");
  });

  it("says so when it owns no nodes", async () => {
    vi.mocked(api.adminUser).mockResolvedValue(detail({ nodes: [] }));
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    expect(within(card("Nodes")).getByText("No nodes")).toBeInTheDocument();
  });

  it("shows the claims to this address, whatever state they are in, with the link's age, an expiry and a bounce as a warning", async () => {
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    const claims = card("Claims to this address");
    const pending = within(claims).getByRole("link", { name: "ret-0099" }).closest("tr") as HTMLElement;
    expect(within(pending).getByText("Pending")).toHaveClass("badge", "info");
    expect(within(pending).getByText("Sent 2h ago, expires in 22h")).not.toHaveClass("detail-warning-inline");
    expect(within(pending).queryByText(/bounced/)).toBeNull();

    const bounced = within(claims).getByText("node-4").closest("tr") as HTMLElement;
    expect(within(bounced).getByText("Mail bounced")).toHaveClass("badge", "offline");
    expect(within(bounced).getByText("Mail to this address bounced")).toHaveClass("detail-warning");
    expect(within(bounced).getByText("Sent 3d ago, expired 2d ago")).toHaveClass("detail-warning-inline");
  });

  it("says so when no other claim carries this address", async () => {
    vi.mocked(api.adminUser).mockResolvedValue(detail({ claims: [] }));
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    expect(within(card("Claims to this address")).getByText("No other claims to this address")).toBeInTheDocument();
  });

  it("asks for the events of this account alone and lists them", async () => {
    vi.mocked(api.adminEvents).mockResolvedValue([
      { ts: nowSeconds - 60, category: "user", message: "Signed in", severity: "info" },
      { ts: nowSeconds - 120, category: "user", message: "Link mail bounced", severity: "warning" },
      { ts: nowSeconds - 180, category: "user", message: "Delivery failed", severity: "error" },
    ]);
    renderAt();

    expect(await within(await findCard("Events")).findByText("Signed in")).toBeInTheDocument();
    expect(api.adminEvents).toHaveBeenCalledWith(200, "u1");
    const events = card("Events");
    expect(within(events).getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["Time", "Severity", "Message"]);
    expect(within(events).getByText("info")).toHaveClass("badge", "info");
    expect(within(events).getByText("warning")).toHaveClass("badge", "warning");
    expect(within(events).getByText("error")).toHaveClass("badge", "offline");
    const row = within(events).getByText("Signed in").closest("tr") as HTMLElement;
    expect(cells(row)[0]).toBe(new Date((nowSeconds - 60) * 1000).toLocaleString());
  });

  it("pages the events past one page", async () => {
    vi.mocked(api.adminEvents).mockResolvedValue(events(30));
    renderAt();

    const log = await findCard("Events");
    expect(await within(log).findByText("Event 0")).toBeInTheDocument();
    expect(within(log).queryByText("Event 25")).toBeNull();
    expect(within(log).getByText(/Page 1 of 2/)).toBeInTheDocument();
    fireEvent.click(within(log).getByRole("button", { name: /Next/ }));
    expect(within(log).getByText("Event 25")).toBeInTheDocument();
    expect(within(log).queryByText("Event 0")).toBeNull();
  });

  it("says so when there are no events", async () => {
    vi.mocked(api.adminEvents).mockResolvedValue([]);
    renderAt();

    expect(await within(await findCard("Events")).findByText("No events for this account")).toBeInTheDocument();
  });

  it("keeps the account on screen when only the events fail", async () => {
    vi.mocked(api.adminEvents).mockRejectedValue(new Error("log unavailable"));
    renderAt();

    expect(await within(await findCard("Events")).findByRole("alert")).toHaveTextContent(
      "Could not load their events: log unavailable",
    );
    expect(screen.getByRole("heading", { level: 1, name: "Ada" })).toBeInTheDocument();
    expect(within(card("Nodes")).getByText("ret-0042")).toBeInTheDocument();
  });

  it("says there is no such account for a 404, with the way back", async () => {
    vi.mocked(api.adminUser).mockRejectedValue(new HttpError(404, "Not Found", { detail: "User not found" }));
    renderAt({ pathname: "/users/nobody", state: { from: "/users?q=ad&page=2" } });

    expect(await screen.findByRole("heading", { level: 1, name: "No such account" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← Back to users" })).toHaveAttribute("href", "/users?q=ad&page=2");
    expect(api.adminEvents).not.toHaveBeenCalled();
  });

  it("reports another failure with a retry, and shows the account once it lands", async () => {
    vi.mocked(api.adminUser).mockRejectedValueOnce(new Error("server down"));
    renderAt();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load this account: server down");
    expect(screen.queryByRole("heading", { name: "No such account" })).toBeNull();
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Ada" })).toBeInTheDocument();
  });

  it("links back to the list as it was left, or to the bare list", async () => {
    const first = renderAt({ pathname: "/users/u1", state: { from: "/users?q=ada&page=3" } });
    expect(await screen.findByRole("link", { name: "← Back to users" })).toHaveAttribute("href", "/users?q=ada&page=3");
    first.unmount();

    renderAt("/users/u1");
    expect(await screen.findByRole("link", { name: "← Back to users" })).toHaveAttribute("href", "/users");
  });

  it("does not follow a link back that leaves the site", async () => {
    renderAt({ pathname: "/users/u1", state: { from: "//elsewhere.example/users" } });
    expect(await screen.findByRole("link", { name: "← Back to users" })).toHaveAttribute("href", "/users");
  });

  it("reloads the account and its events after a deactivation", async () => {
    vi.mocked(api.adminDeactivateUser).mockResolvedValue({ ...ADA, is_active: false });
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });
    vi.mocked(api.adminUser).mockResolvedValue(detail({ user: { ...ADA, is_active: false } }));

    fireEvent.click(screen.getByRole("button", { name: "Deactivate" }));
    fireEvent.click(await screen.findByRole("button", { name: "Deactivate" }));

    await waitFor(() => expect(screen.getByText("Deactivated", { selector: ".user-detail-badges .badge" })).toHaveClass("offline"));
    expect(api.adminUser).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(api.adminEvents).toHaveBeenCalledTimes(2));
  });

  it("says what a delete did, in place, with the way back to the list", async () => {
    vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 2 });
    renderAt({ pathname: "/users/u1", state: { from: "/users?q=ada" } });
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this account?" });
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    const notice = await screen.findByText("Deleted ada@example.com. 2 nodes released.");
    expect(notice).toHaveAttribute("role", "status");
    await waitFor(() => expect(notice).toHaveFocus());
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    expect(api.adminDeleteUser).toHaveBeenCalledWith("u1");

    fireEvent.click(screen.getByRole("link", { name: "← Back to users" }));
    expect(await screen.findByText("The list")).toBeInTheDocument();
    expect(screen.getByTestId("address")).toHaveTextContent("/users");
  });

  it("says the account is gone when an action finds it deleted elsewhere", async () => {
    vi.mocked(api.adminSendSignInLink).mockRejectedValue(new HttpError(404, "Not Found", { detail: "User not found" }));
    renderAt();
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    fireEvent.click(screen.getByRole("button", { name: "Send sign-in link" }));

    expect(await screen.findByText("The account for ada@example.com no longer exists.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← Back to users" })).toBeInTheDocument();
  });

  it("starts afresh for another account instead of showing the last one's rows", async () => {
    const other = { ...ADA, id: "u2", name: "Bob", email: "bob@example.com" };
    vi.mocked(api.adminUser).mockImplementation((id: string) =>
      id === "u2" ? new Promise(() => {}) : Promise.resolve(detail()),
    );
    render(
      <MemoryRouter initialEntries={["/users/u1"]}>
        <Link to="/users/u2">Bob</Link>
        <Routes>
          <Route path="/users/:userId" element={<UserDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { level: 1, name: "Ada" });

    fireEvent.click(screen.getByRole("link", { name: "Bob" }));

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Ada" })).toBeNull();
    expect(api.adminUser).toHaveBeenLastCalledWith(other.id);
  });
});

describe("the route", () => {
  const routes = consoleRoutes(true, { syntheticFleet: false });

  function Header() {
    const { pathname } = useLocation();
    return <h2>{pageTitle(pathname, routes)}</h2>;
  }

  function renderApp(path: string) {
    vi.mocked(api.adminUsers).mockResolvedValue({ users: [ADA], total: 1 });
    return render(
      <MemoryRouter initialEntries={[path]}>
        <Header />
        <Suspense fallback={<span>Loading page</span>}>
          <Routes>
            {routes.map((route) => ("element" in route ? <Route key={route.path} path={route.path} element={route.element} /> : null))}
          </Routes>
        </Suspense>
      </MemoryRouter>,
    );
  }

  it("resolves /users/:userId to the account's page under the title User Detail", async () => {
    renderApp("/users/u1");

    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("User Detail");
    expect(await screen.findByRole("heading", { level: 1, name: "Ada" })).toBeInTheDocument();
    expect(api.adminUser).toHaveBeenCalledWith("u1");
  });

  it("still renders the list at /users", async () => {
    renderApp("/users");

    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("User Management");
    expect(await screen.findByRole("heading", { level: 1, name: "User Management" })).toBeInTheDocument();
    expect(api.adminUser).not.toHaveBeenCalled();
    expect(await screen.findByRole("link", { name: "Ada" })).toHaveAttribute("href", "/users/u1");
  });
});

/** The card is there from the first answer; the events fill it in after. */
async function findCard(title: string) {
  return waitFor(() => card(title));
}
