import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: {
    adminFleet: vi.fn(),
  },
}));

import { api } from "../api/client";
import FleetPage from "../pages/admin/FleetPage";
import fleetCss from "../pages/admin/fleet/fleet.css?raw";
import type { AdminFleet, AdminFleetMirror, AdminFleetRow } from "../types";
import { declarations } from "./paletteTokens";

const NOW = new Date("2026-10-15T12:00:00Z");
const HOUR = 3600;
const nowSeconds = NOW.getTime() / 1000;

const BLANK: AdminFleetRow = {
  node_id: null,
  presence: "registry_only",
  node_ref: null,
  status: null,
  first_seen_at: null,
  last_seen_at: null,
  claim_email: null,
  claim_verified: null,
  contact_email: null,
  email_state: "none",
  mender_device_id: null,
  auth_status: null,
  device_type: null,
  artifact_name: null,
  inventory_updated_at: null,
  present: null,
  never_registered: false,
  snapshot_issue: null,
};

const BOTH: AdminFleetRow = {
  ...BLANK,
  node_id: "ret1111",
  presence: "both",
  node_ref: "ref-1111",
  status: "online",
  last_seen_at: nowSeconds - 3 * HOUR,
  claim_email: "ada@example.com",
  claim_verified: true,
  email_state: "claimed_verified",
  mender_device_id: "dev-1111",
  auth_status: "accepted",
  device_type: "retina-rpi",
  artifact_name: "retina-node-1.2.0",
  inventory_updated_at: nowSeconds - 2 * HOUR,
  present: true,
};

const REGISTRY: AdminFleetRow = {
  ...BLANK,
  node_id: "ret2222",
  presence: "registry_only",
  node_ref: "ref-2222",
  status: "offline",
  contact_email: "Site@Example.org",
  email_state: "contact_only",
};

const MENDER: AdminFleetRow = {
  ...BLANK,
  node_id: "nde3333",
  presence: "mender_only",
  mender_device_id: "dev-3333",
  auth_status: "pending",
  device_type: "retina-pi5",
  artifact_name: "retina-node-1.3.0",
  present: true,
};

const NEVER: AdminFleetRow = {
  ...MENDER,
  node_id: "ret4444",
  mender_device_id: "dev-4444",
  never_registered: true,
};

const REMOVED: AdminFleetRow = {
  ...BOTH,
  node_id: "ret5555",
  node_ref: "ref-5555",
  mender_device_id: "dev-5555",
  claim_email: null,
  claim_verified: null,
  email_state: "none",
  present: false,
};

const NO_ID: AdminFleetRow = {
  ...MENDER,
  node_id: null,
  mender_device_id: "dev-orphan-9",
};

const SHARED: AdminFleetRow = {
  ...BOTH,
  node_id: "ret1a2b3c4d",
  node_ref: "ref-shared",
  mender_device_id: "dev-shared-a",
  snapshot_issue: "node_id shared by 2 devices",
};

const MANY_ISSUES: AdminFleetRow = {
  ...MENDER,
  node_id: "ret2b3c4d5e",
  mender_device_id: "dev-many",
  auth_status: "accepted",
  snapshot_issue: "accepted without an auth set; node_id too long",
};

const GONE_ISSUE: AdminFleetRow = {
  ...REMOVED,
  node_id: "ret3a4b5c6d",
  mender_device_id: "dev-gone",
  snapshot_issue: "mender_device_id repeated in the snapshot",
};

const FRESH: AdminFleetMirror = {
  last_snapshot_at: nowSeconds - 60,
  age_s: 60,
  max_age_s: 300,
  stale: false,
  stale_reason: null,
  generated_at: nowSeconds - 62,
  source: "mender-auto-accept@central",
  device_count: 41,
  mender_source: "mirror",
};

function fleet(rows: AdminFleetRow[], mirror: Partial<AdminFleetMirror> = {}): AdminFleet {
  const counts = { both: 0, registry_only: 0, mender_only: 0 };
  for (const row of rows) counts[row.presence] += 1;
  return { mirror: { ...FRESH, ...mirror }, counts, rows };
}

const ALL = [BOTH, REGISTRY, MENDER, NEVER, REMOVED, NO_ID];

const HEADERS = ["Node", "Registry", "Last seen", "Email", "Mender", "Device", "Inventory", "Presence"];

function renderPage() {
  return render(
    <MemoryRouter>
      <FleetPage />
    </MemoryRouter>,
  );
}

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function show(answer: AdminFleet) {
  vi.mocked(api.adminFleet).mockResolvedValue(answer);
  renderPage();
  await flush();
}

/** The body row whose first cell holds `id`. */
function rowOf(id: string): HTMLElement {
  const cell = screen.getByText(id, { selector: ".fleet-id" });
  return cell.closest("tr") as HTMLElement;
}

const cell = (row: HTMLElement, label: string) => row.querySelector(`td[data-label="${label}"]`) as HTMLElement;
const statValue = (label: string) =>
  screen.getByText(label, { selector: ".stat-label" }).closest(".stat-card")?.querySelector(".stat-value")?.textContent;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.resetAllMocks();
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("FleetPage", () => {
  it("asks for the fleet and names each column", async () => {
    await show(fleet(ALL));

    expect(api.adminFleet).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "Fleet" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).toEqual(HEADERS);
    // A phone reads each value under its own label, the header's own word.
    const labels = Array.from(rowOf("ret1111").querySelectorAll("td")).map((td) => td.dataset.label);
    expect(labels).toEqual(HEADERS);
    expect(cell(rowOf("ret1111"), "Device")).toHaveTextContent("retina-node-1.2.0");
    expect(cell(rowOf("ret1111"), "Device")).toHaveTextContent("retina-rpi");
  });

  it("counts the three presence states in stat cards", async () => {
    await show(fleet(ALL));

    expect(statValue("In both")).toBe("2");
    expect(statValue("Registry only")).toBe("1");
    expect(statValue("Mender only")).toBe("3");
  });

  it("gives each presence state its own badge", async () => {
    await show(fleet(ALL));

    const both = within(cell(rowOf("ret1111"), "Presence")).getByText("Both");
    expect(both).toHaveClass("badge", "online");
    const registry = within(cell(rowOf("ret2222"), "Presence")).getByText("Registry only");
    expect(registry).toHaveClass("badge", "warning");
    const mender = within(cell(rowOf("nde3333"), "Presence")).getByText("Mender only");
    expect(mender).toHaveClass("badge", "warning");
  });

  it("words each email state, with the address that state speaks of", async () => {
    await show(fleet(ALL));

    const claimed = cell(rowOf("ret1111"), "Email");
    expect(claimed).toHaveTextContent("ada@example.com");
    expect(within(claimed).getByText("claimed & verified")).toBeInTheDocument();

    const contact = cell(rowOf("ret2222"), "Email");
    expect(contact).toHaveTextContent("Site@Example.org");
    expect(within(contact).getByText("contact only")).toBeInTheDocument();

    const none = cell(rowOf("nde3333"), "Email");
    expect(within(none).getByText("none")).toBeInTheDocument();
    expect(none.textContent).toBe("none");
  });

  it("flags a Mender-only node with the ret prefix as enrolled, never registered", async () => {
    await show(fleet(ALL));

    expect(within(rowOf("ret4444")).getByText("enrolled, never registered")).toHaveClass("badge");
    // Another Mender-only node is not that, nor is a registered one.
    expect(within(rowOf("nde3333")).queryByText("enrolled, never registered")).toBeNull();
    expect(within(rowOf("ret1111")).queryByText("enrolled, never registered")).toBeNull();
    expect(screen.getAllByText("enrolled, never registered")).toHaveLength(1);
  });

  it("says a device Mender removed is removed, and badges the others by their auth status", async () => {
    await show(fleet(ALL));

    expect(within(cell(rowOf("ret5555"), "Mender")).getByText("removed from Mender")).toBeInTheDocument();
    expect(within(cell(rowOf("ret5555"), "Mender")).queryByText("accepted")).toBeNull();
    expect(within(cell(rowOf("ret1111"), "Mender")).getByText("accepted")).toHaveClass("badge", "online");
    expect(within(cell(rowOf("nde3333"), "Mender")).getByText("pending")).toHaveClass("badge", "warning");
    // A node Mender has not seen has nothing there.
    expect(cell(rowOf("ret2222"), "Mender")).toHaveTextContent("—");
    expect(screen.getAllByText("removed from Mender")).toHaveLength(1);
  });

  it("shows the Mender device id for a row with no node id", async () => {
    await show(fleet(ALL));

    const row = rowOf("dev-orphan-9");
    expect(cell(row, "Node")).toHaveTextContent("dev-orphan-9");
    expect(within(row).getByText("Mender only")).toBeInTheDocument();
  });

  it("shows last seen and inventory updated as relative times, and Never for no sighting", async () => {
    await show(fleet(ALL));

    const seen = cell(rowOf("ret1111"), "Last seen");
    expect(seen).toHaveTextContent("3h ago");
    expect(seen.querySelector("time")).toHaveAttribute("datetime", new Date((nowSeconds - 3 * HOUR) * 1000).toISOString());
    expect(cell(rowOf("ret1111"), "Inventory")).toHaveTextContent("2h ago");
    expect(cell(rowOf("ret2222"), "Last seen")).toHaveTextContent("Never");
    expect(cell(rowOf("ret2222"), "Inventory")).toHaveTextContent("—");
  });

  it("puts the node ref under the node id", async () => {
    await show(fleet(ALL));

    expect(cell(rowOf("ret1111"), "Node")).toHaveTextContent("ret1111ref-1111");
  });

  it("states the mirror, its source, its device count and the registration source", async () => {
    await show(fleet(ALL));

    const line = document.querySelector(".fleet-mirror") as HTMLElement;
    expect(line).toHaveTextContent("last snapshot 1m ago from mender-auto-accept@central, 41 devices");
    expect(line).toHaveTextContent("Registration source: mirror");
  });

  it("says so when no snapshot has arrived", async () => {
    await show(
      fleet([], {
        last_snapshot_at: null,
        age_s: null,
        stale: true,
        stale_reason: "no snapshot yet",
        source: null,
        device_count: null,
      }),
    );

    expect(document.querySelector(".fleet-mirror")).toHaveTextContent("no snapshot yet");
    expect(screen.getByText("No nodes in the registry or in Mender yet")).toBeInTheDocument();
  });

  it("polls every 30 seconds", async () => {
    await show(fleet(ALL));
    expect(api.adminFleet).toHaveBeenCalledTimes(1);

    await flush(29_000);
    expect(api.adminFleet).toHaveBeenCalledTimes(1);
    await flush(1_000);
    expect(api.adminFleet).toHaveBeenCalledTimes(2);
  });
});

describe("FleetPage stale banner", () => {
  const STALE = {
    stale: true,
    stale_reason: "last snapshot 10800 s ago, limit 300 s",
    last_snapshot_at: nowSeconds - 3 * HOUR,
    age_s: 3 * HOUR,
    max_age_s: 300,
  };

  it("is absent while the mirror is fresh", async () => {
    await show(fleet(ALL));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("is an error under the mirror source: registration refuses until a fresh snapshot", async () => {
    await show(fleet(ALL, { ...STALE, mender_source: "mirror" }));

    const banner = screen.getByRole("alert");
    expect(banner).toHaveClass("notice", "error");
    expect(banner).toHaveTextContent("The last Mender snapshot arrived 3.0h ago (limit 5m).");
    expect(banner).toHaveTextContent("Registration is refusing nodes until a fresh snapshot arrives.");
    expect(banner).toHaveTextContent("mender-auto-accept timer");
    expect(banner).not.toHaveTextContent("asks Mender directly");
  });

  it.each(["api", "shadow"])("is a warning under the %s source: registration still asks Mender", async (source) => {
    await show(fleet(ALL, { ...STALE, mender_source: source }));

    const banner = screen.getByRole("alert");
    expect(banner).toHaveClass("notice");
    expect(banner).not.toHaveClass("error");
    expect(banner).toHaveTextContent("Registration still asks Mender directly.");
    expect(banner).toHaveTextContent("mender-auto-accept timer");
    expect(banner).not.toHaveTextContent("refusing");
  });

  it("says no snapshot has arrived when there is none", async () => {
    await show(fleet([], { ...STALE, stale_reason: "no snapshot yet", last_snapshot_at: null, age_s: null }));

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("No Mender snapshot has arrived yet.");
    expect(banner).not.toHaveTextContent("The last Mender snapshot");
    expect(banner).toHaveClass("error");
  });

  it("names a malformed MENDER_MIRROR_MAX_AGE_S and quotes no limit", async () => {
    await show(
      fleet(ALL, {
        ...STALE,
        stale_reason: "MENDER_MIRROR_MAX_AGE_S='5m' is not a number",
        max_age_s: null,
        mender_source: "api",
      }),
    );

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("MENDER_MIRROR_MAX_AGE_S is malformed");
    expect(banner).toHaveTextContent("The last Mender snapshot arrived 3.0h ago.");
    expect(banner).not.toHaveTextContent("limit");
  });

  it("says registration is refusing when MENDER_SOURCE is not recognised", async () => {
    await show(fleet(ALL, { ...STALE, mender_source: "mirrr" }));

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("Registration is refusing nodes because MENDER_SOURCE is not recognised.");
    expect(banner).toHaveClass("error");
    expect(banner).not.toHaveTextContent("asks Mender directly");
  });

  it("says registration is refusing under an unrecognised MENDER_SOURCE while the mirror is fresh", async () => {
    await show(fleet(ALL, { mender_source: "mirrr" }));

    const banner = screen.getByRole("alert");
    expect(banner).toHaveClass("notice", "error");
    // Only the source is wrong: nothing about the snapshot or its timer.
    expect(banner).toHaveTextContent(/^Registration is refusing nodes because MENDER_SOURCE is not recognised\.$/);
    expect(document.querySelector(".fleet-stale-reason")).toBeNull();
  });

  it.each(["api", "shadow", "mirror"])("is absent while the mirror is fresh under the %s source", async (source) => {
    await show(fleet(ALL, { mender_source: source }));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("sets the server's reason on its own line under the wording", async () => {
    await show(fleet(ALL, { ...STALE, mender_source: "mirror" }));

    const reason = document.querySelector(".fleet-stale-reason") as HTMLElement;
    expect(reason).toHaveTextContent("last snapshot 10800 s ago, limit 300 s");
    expect(screen.getByRole("alert")).toContainElement(reason);
  });

  it.each([
    ["no snapshot yet", { last_snapshot_at: null, age_s: null }],
    ["last snapshot 412 s ago, limit 300 s", { last_snapshot_at: nowSeconds - 412, age_s: 412 }],
    ["MENDER_MIRROR_MAX_AGE_S='5m' is not a number", { max_age_s: null }],
  ])("shows the reason %s", async (stale_reason, extra) => {
    await show(fleet(ALL, { ...STALE, stale_reason, ...extra }));

    expect(document.querySelector(".fleet-stale-reason")).toHaveTextContent(stale_reason);
  });

  it("adds no reason line while the server sends none", async () => {
    await show(fleet(ALL, { ...STALE, stale_reason: null }));

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(document.querySelector(".fleet-stale-reason")).toBeNull();
  });

  it("handles a snapshot from the future: no negative age, the reason explains", async () => {
    const reason = "last snapshot is 95 s in the future, the server clock went back";
    await show(fleet(ALL, { ...STALE, stale_reason: reason, last_snapshot_at: nowSeconds + 95, age_s: -95 }));

    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("The last Mender snapshot is dated in the future.");
    expect(banner).not.toHaveTextContent(/-\d|arrived/);
    expect(banner).toHaveTextContent(reason);
    const line = document.querySelector(".fleet-mirror") as HTMLElement;
    expect(line).toHaveTextContent("last snapshot dated in the future");
    expect(line).not.toHaveTextContent(/-\d|just now|ago/);
  });

  it("leaves with the next fresh answer", async () => {
    vi.mocked(api.adminFleet)
      .mockResolvedValueOnce(fleet(ALL, { ...STALE, mender_source: "mirror" }))
      .mockResolvedValue(fleet(ALL));
    renderPage();
    await flush();
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await flush(30_000);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("FleetPage snapshot issues", () => {
  const WITH_ISSUES = [BOTH, SHARED, MANY_ISSUES, GONE_ISSUE];

  it("marks a row with an issue not usable, beside its auth status, and says why", async () => {
    await show(fleet(WITH_ISSUES));

    const mender = cell(rowOf("ret1a2b3c4d"), "Mender");
    expect(within(mender).getByText("not usable")).toHaveClass("badge", "warning");
    expect(within(mender).getByText("accepted")).toHaveClass("badge", "online");
    const why = within(mender).getByText("node_id shared by 2 devices");
    expect(why).toHaveClass("fleet-sub");
  });

  it("prints several issues in one line, as the server joins them", async () => {
    await show(fleet(WITH_ISSUES));

    const mender = cell(rowOf("ret2b3c4d5e"), "Mender");
    expect(within(mender).getByText("accepted without an auth set; node_id too long")).toBeInTheDocument();
    expect(within(mender).getAllByText("not usable")).toHaveLength(1);
  });

  it("keeps the badge on a device Mender removed", async () => {
    await show(fleet(WITH_ISSUES));

    const mender = cell(rowOf("ret3a4b5c6d"), "Mender");
    expect(within(mender).getByText("removed from Mender")).toBeInTheDocument();
    expect(within(mender).getByText("not usable")).toBeInTheDocument();
  });

  it("leaves a row without an issue as it was", async () => {
    await show(fleet(WITH_ISSUES));

    const mender = cell(rowOf("ret1111"), "Mender");
    expect(within(mender).queryByText("not usable")).toBeNull();
    expect(mender.textContent).toBe("accepted");
    expect(screen.getAllByText("not usable")).toHaveLength(3);
  });

  it("searches the issue text", async () => {
    await show(fleet(WITH_ISSUES));

    fireEvent.change(screen.getByRole("searchbox", { name: "Search the fleet" }), { target: { value: "SHARED BY 2" } });
    expect(Array.from(document.querySelectorAll("tbody .fleet-id")).map((el) => el.textContent)).toEqual([
      "ret1a2b3c4d",
    ]);
  });

  it("counts the rows with an issue in a Not usable card", async () => {
    await show(fleet(WITH_ISSUES));

    expect(statValue("Not usable")).toBe("3");
  });

  it("shows no Not usable card when every entry is usable", async () => {
    await show(fleet(ALL));

    expect(screen.queryByText("Not usable", { selector: ".stat-label" })).toBeNull();
  });
});

describe("FleetPage search and filter", () => {
  const searchbox = () => screen.getByRole("searchbox", { name: "Search the fleet" });
  const presence = () => screen.getByRole("combobox", { name: "Filter by presence" });
  const ids = () => Array.from(document.querySelectorAll("tbody .fleet-id")).map((el) => el.textContent);

  it("lists every row to begin with", async () => {
    await show(fleet(ALL));

    expect(ids()).toEqual(["ret1111", "ret2222", "nde3333", "ret4444", "ret5555", "dev-orphan-9"]);
  });

  it.each([
    ["node id", "ret2222", ["ret2222"]],
    ["node ref", "REF-1111", ["ret1111"]],
    ["claim email", "ada@", ["ret1111"]],
    ["contact email, in any case", "SITE@example", ["ret2222"]],
    ["artifact", "1.3.0", ["nde3333", "ret4444", "dev-orphan-9"]],
    ["device type", "pi5", ["nde3333", "ret4444", "dev-orphan-9"]],
  ])("searches the %s", async (_, text, expected) => {
    await show(fleet(ALL));

    fireEvent.change(searchbox(), { target: { value: text } });
    expect(ids()).toEqual(expected);
  });

  it("says when nothing matches", async () => {
    await show(fleet(ALL));

    fireEvent.change(searchbox(), { target: { value: "zzz" } });
    expect(screen.getByText("No nodes match")).toBeInTheDocument();
  });

  it.each([
    ["both", ["ret1111", "ret5555"]],
    ["registry_only", ["ret2222"]],
    ["mender_only", ["nde3333", "ret4444", "dev-orphan-9"]],
    ["all", ["ret1111", "ret2222", "nde3333", "ret4444", "ret5555", "dev-orphan-9"]],
  ])("filters by presence %s", async (value, expected) => {
    await show(fleet(ALL));

    fireEvent.change(presence(), { target: { value } });
    expect(ids()).toEqual(expected);
  });

  it("combines the search with the presence filter", async () => {
    await show(fleet(ALL));

    fireEvent.change(presence(), { target: { value: "mender_only" } });
    fireEvent.change(searchbox(), { target: { value: "ret4" } });
    expect(ids()).toEqual(["ret4444"]);
  });

  it("pages 25 rows at a time and returns to the first page when the filter changes", async () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      ...BOTH,
      node_id: `ret${String(i).padStart(4, "0")}`,
      node_ref: null,
    }));
    await show(fleet(many));

    expect(ids()).toHaveLength(25);
    expect(screen.getByText("Page 1 of 2 (30 nodes)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(ids()).toEqual(["ret0025", "ret0026", "ret0027", "ret0028", "ret0029"]);
    expect(screen.getByText("Showing 26–30 of 30")).toBeInTheDocument();

    fireEvent.change(searchbox(), { target: { value: "ret000" } });
    expect(ids()).toHaveLength(10);
    expect(screen.queryByText(/Page \d of/)).toBeNull();
  });

  it("goes back to the first page on a new search or presence filter", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...(i < 55 ? BOTH : REGISTRY),
      node_id: `ret${String(i).padStart(4, "0")}`,
      node_ref: null,
    }));
    await show(fleet(many));

    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText(/Page 2 of 3/)).toBeInTheDocument();
    fireEvent.change(searchbox(), { target: { value: "ret00" } });
    expect(screen.getByText(/Page 1 of 3/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    fireEvent.change(presence(), { target: { value: "both" } });
    expect(screen.getByText(/Page 1 of 3/)).toBeInTheDocument();
  });
});

describe("FleetPage registry status", () => {
  const ACTIVE: AdminFleetRow = { ...BOTH, node_id: "ret5e6f7a8b", node_ref: "ref-active", status: "active" };
  const BLOCKED: AdminFleetRow = { ...BOTH, node_id: "ret0a1b2c3d", node_ref: "ref-blocked", status: "blocked" };
  const RETIRED: AdminFleetRow = { ...REGISTRY, node_id: "ret9c0d1e2f", node_ref: "ref-retired", status: "retired" };
  const ROWS = [ACTIVE, BLOCKED, RETIRED, MENDER, NEVER];
  const TONES = ["online", "warning", "offline", "private", "info"];

  it.each([
    ["ret5e6f7a8b", "active", "online"],
    ["ret0a1b2c3d", "blocked", "warning"],
    ["ret9c0d1e2f", "retired", "private"],
  ])("shows %s as %s, in the %s tone", async (id, status, tone) => {
    await show(fleet(ROWS));

    const registry = cell(rowOf(id), "Registry");
    // The registry's own word, not a liveness word read into it.
    expect(registry.textContent).toBe(status);
    const badge = within(registry).getByText(status);
    expect(badge).toHaveClass("badge", tone);
    for (const other of TONES.filter((t) => t !== tone)) expect(badge).not.toHaveClass(other);
  });

  it("shows a status it has no tone for as the registry words it, in the neutral tone", async () => {
    await show(fleet([{ ...ACTIVE, status: "quarantined" }]));

    const badge = within(cell(rowOf("ret5e6f7a8b"), "Registry")).getByText("quarantined");
    expect(badge).toHaveClass("badge", "private");
  });

  it("gives a Mender-only node a dash, or enrolled, never registered", async () => {
    await show(fleet(ROWS));

    expect(cell(rowOf("nde3333"), "Registry").textContent).toBe("—");
    const never = within(cell(rowOf("ret4444"), "Registry")).getByText("enrolled, never registered");
    expect(never).toHaveClass("badge", "warning");
  });
});

describe("FleetPage layout", () => {
  const ADDRESS = "fleet.operations.coordinator@example.org";
  const ARTIFACT = "retina-node-1.2.0-rc.1+build.7";
  const WIDE: AdminFleetRow = {
    ...BOTH,
    node_id: "ret2b3c4d5e",
    node_ref: "ndeexample00001",
    claim_email: ADDRESS,
    artifact_name: ARTIFACT,
    device_type: "raspberrypi5-industrial",
  };
  const TYPE_ONLY: AdminFleetRow = { ...MENDER, node_id: "ret0a1b2c3d", artifact_name: null };

  /** The element in a row's cell that shows `text`. */
  const line = (id: string, label: string, text: string) => within(cell(rowOf(id), label)).getByText(text);

  it("keeps an address on one line, the whole of it in the title", async () => {
    await show(fleet([WIDE]));

    const address = line("ret2b3c4d5e", "Email", ADDRESS);
    expect(address).toHaveClass("fleet-email");
    expect(address).toHaveAttribute("title", ADDRESS);
    expect(within(cell(rowOf("ret2b3c4d5e"), "Email")).getByText("claimed & verified")).toHaveClass("fleet-sub");
  });

  it("keeps the node id, its ref, the artifact and the device type each on one titled line", async () => {
    await show(fleet([WIDE]));

    for (const [label, text, kind] of [
      ["Node", "ret2b3c4d5e", "fleet-id"],
      ["Node", "ndeexample00001", "fleet-ref"],
      ["Device", ARTIFACT, "fleet-artifact"],
      ["Device", "raspberrypi5-industrial", "fleet-type"],
    ]) {
      const el = line("ret2b3c4d5e", label, text);
      expect(el, text).toHaveClass(kind);
      expect(el, text).toHaveAttribute("title", text);
    }
  });

  it("puts the artifact in mono over the device type, with a dash for what Mender did not report", async () => {
    await show(fleet([WIDE, TYPE_ONLY, REGISTRY]));

    const device = cell(rowOf("ret2b3c4d5e"), "Device");
    const [artifact, type] = Array.from(device.querySelectorAll(".fleet-artifact, .fleet-type"));
    expect(artifact).toHaveTextContent(ARTIFACT);
    expect(artifact).toHaveClass("fleet-artifact", "mono");
    expect(type).toHaveTextContent("raspberrypi5-industrial");
    expect(type).toHaveClass("fleet-type", "fleet-sub");
    expect(cell(rowOf("ret0a1b2c3d"), "Device").textContent).toBe("—retina-pi5");
    expect(cell(rowOf("ret2222"), "Device").textContent).toBe("—");
  });

  // What the desktop table gives each column, read from the stylesheet.
  const css = fleetCss.replace(/\/\*[\s\S]*?\*\//g, "");
  const desktop = css.slice(css.indexOf("@media (min-width: 641px)"), css.indexOf("@media (max-width: 640px)"));
  const widths = new Map(
    Array.from(desktop.matchAll(/th:nth-child\((\d+)\)\s*\{\s*width:\s*([\d.]+)(px|%);\s*\}/g), ([, n, value, unit]) => [
      HEADERS[Number(n) - 1],
      (table: number) => (unit === "%" ? (Number(value) * table) / 100 : Number(value)),
    ]),
  );

  /** Each flat rule in `sheet`: its selectors and its declarations. */
  const rules = (sheet: string) =>
    Array.from(sheet.matchAll(/([^{}]+)\{([^{}]*)\}/g), ([, prelude, body]) => ({
      selectors: prelude.split(",").map((s) => s.trim()),
      decls: Object.fromEntries(
        body
          .split(";")
          .map((d) => d.split(":").map((s) => s.trim()))
          .filter(([name]) => name),
      ) as Record<string, string>,
    }));

  it("ends an id, ref, address, artifact or device type in an ellipsis, and wraps it on a phone instead", () => {
    const ONE_LINE = ["fleet-id", "fleet-ref", "fleet-email", "fleet-artifact", "fleet-type"].map((c) => `.fleet-card .${c}`);
    const everywhere = rules(css.slice(0, css.indexOf("@media")));
    const clipped = everywhere.find((r) => r.selectors.includes(".fleet-card .fleet-email") && r.decls["white-space"]);
    expect(clipped?.selectors).toEqual(ONE_LINE);
    expect(clipped?.decls).toMatchObject({
      "max-width": "100%",
      overflow: "hidden",
      "text-overflow": "ellipsis",
      "white-space": "nowrap",
    });
    // A touch screen has no hover to show the title by.
    const phone = rules(css.slice(css.indexOf("@media (max-width: 640px)")));
    const wrapped = phone.find((r) => r.selectors.includes(".fleet-card .fleet-email"));
    expect(wrapped?.selectors).toEqual(ONE_LINE);
    expect(wrapped?.decls).toMatchObject({ "white-space": "normal", "overflow-wrap": "anywhere" });
  });

  it("lays the desktop table out fixed, sizing every column but Email, which takes the rest", () => {
    expect(desktop).toMatch(/^@media \(min-width: 641px\)/);
    expect(declarations(desktop, ".fleet-card table {")["table-layout"]).toBe("fixed");
    expect(HEADERS.filter((h) => !widths.has(h))).toEqual(["Email"]);
    expect(widths.size).toBe(HEADERS.length - 1);
  });

  it("fits the eight columns in the 1140px of card a 1440 screen leaves beside the sidebar", () => {
    // 1440, less the 250px sidebar, the page's 24px padding a side and the card's 1px border a side.
    const TABLE = 1440 - 250 - 2 * 24 - 2;
    expect(declarations(desktop, ".fleet-card td:not(.table-note) {")).toEqual({
      "padding-left": "8px",
      "padding-right": "8px",
    });
    expect(declarations(desktop, ".fleet-card td:first-child:not(.table-note) {")["padding-left"]).toBe("16px");
    expect(declarations(desktop, ".fleet-card td:last-child:not(.table-note) {")["padding-right"]).toBe("16px");
    // The widest value each column holds, measured in the system font, and the
    // padding around it: 8px a side, 16px at the table's two edges.
    const NEED: Record<string, number> = {
      Node: 108 + 24, // a node ref, 15 mono characters
      Registry: 181 + 16, // the "enrolled, never registered" badge
      "Last seen": 66 + 16, // its header
      Email: 172 + 16, // a 24-character address
      Mender: 160 + 16, // the "removed from Mender" badge
      Device: 94 + 16, // an artifact such as owl-os-1.4.12
      Inventory: 71 + 16, // its header
      Presence: 107 + 24, // the "Registry only" badge
    };
    let sized = 0;
    widths.forEach((width) => (sized += width(TABLE)));
    for (const column of HEADERS) {
      const width = column === "Email" ? TABLE - sized : widths.get(column)!(TABLE);
      expect(width, column).toBeGreaterThanOrEqual(NEED[column]);
    }
  });
});
