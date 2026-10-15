import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: {
    adminNodeRefs: vi.fn(),
    adminNodeClaim: vi.fn(),
    adminSetNodeOwner: vi.fn(),
    adminUsers: vi.fn(),
  },
}));

import { api } from "../api/client";
import { AdminClaimCard } from "../pages/admin/claims/AdminClaimCard";
import type { AdminClaim, AdminUser } from "../types";

const NOW = 1_790_000_000;
const HOUR = 3600;
const DAY = 24 * HOUR;

const REF = "nde0a1b2c3d4e5f";
const ID = "ret0a1b2c3d";
const ADA = "ada@example.com";
const ADA_ID = "6f0e1d2c-3b4a-4958-8776-655443322110";
const ADA_OWNER = { user_id: ADA_ID, email: ADA, name: "Ada" };

const BOB: AdminUser = {
  id: "0a1b2c3d-4e5f-4a6b-8c7d-8e9fa0b1c2d3",
  email: "bob@example.com",
  name: "Bob",
  role: "user",
  is_superuser: false,
  is_active: true,
  created_at: NOW - 10 * DAY,
  last_seen_at: NOW - DAY,
  is_verified: true,
  node_count: 1,
};

function claim(overrides: Partial<AdminClaim> = {}): AdminClaim {
  return {
    node_id: ID,
    node_ref: REF,
    status: "unclaimed",
    owner: null,
    email: null,
    verified: false,
    undeliverable: false,
    updated_at: null,
    owned_since: null,
    challenge: null,
    ...overrides,
  };
}

const OWNED_BY_ADA = claim({
  status: "owned",
  owner: ADA_OWNER,
  email: ADA,
  verified: true,
  updated_at: NOW - 3 * HOUR,
  owned_since: NOW - 2 * DAY,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const realLocation = window.location;

function onHost(hostname: string) {
  Object.defineProperty(window, "location", {
    value: { hostname, pathname: `/nodes/${REF}`, search: "", hash: "", href: `https://${hostname}/nodes/${REF}` },
    writable: true,
    configurable: true,
  });
}

function renderCard(name = REF) {
  return render(
    <MemoryRouter initialEntries={[`/nodes/${name}`]}>
      <Routes>
        <Route path="/nodes/:nodeId" element={<AdminClaimCard nodeRef={name} />} />
        <Route path="/users/:userId" element={<p>Account page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The card once its own chunk and its claim are in, by its badge. */
async function loaded(badge: string) {
  const card = await screen.findByRole("region", { name: "Claim" });
  await within(card).findByText(badge, { selector: ".badge" });
  return card;
}

/** The reading beside a label in the card's table. */
function reading(label: string): HTMLElement {
  return screen.getByText(label, { selector: "td" }).nextElementSibling as HTMLElement;
}

const status = () => within(screen.getByRole("region", { name: "Claim" })).getByRole("status");

beforeEach(() => {
  vi.resetAllMocks();
  // Only the clock: the dialog's search waits on a real timer.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW * 1000);
  onHost("admin.localhost");
  vi.mocked(api.adminNodeRefs).mockResolvedValue({ [REF]: ID, "other-ref": "ret00000000" });
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

describe("on the owners' surface", () => {
  it.each(["localhost", "retina.fm"])("renders nothing on %s and asks nothing", async (hostname) => {
    onHost(hostname);

    const { container } = renderCard();
    // Time for the card's own chunk and its first read, were it shown.
    await act(async () => {
      await import("../pages/admin/claims/ClaimCard");
      await new Promise((done) => setTimeout(done, 0));
    });

    expect(container).toBeEmptyDOMElement();
    expect(api.adminNodeRefs).not.toHaveBeenCalled();
    expect(api.adminNodeClaim).not.toHaveBeenCalled();
  });
});

describe("what the card says about each claim", () => {
  it("reads the node's claim by the id the admin node map gives its name", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(OWNED_BY_ADA);
    renderCard();

    await loaded("Owned");

    expect(api.adminNodeClaim).toHaveBeenCalledWith(ID);
    expect(reading("Node id")).toHaveTextContent(ID);
  });

  it("an owner who claimed by mail: owned, linked to their account, and verified", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(OWNED_BY_ADA);
    renderCard();
    await loaded("Owned");

    const owner = within(reading("Owner")).getByRole("link", { name: ADA });
    expect(owner).toHaveAttribute("href", `/users/${ADA_ID}`);
    expect(reading("Claim address")).toHaveTextContent(ADA);
    expect(reading("Verified")).toHaveTextContent("Yes");
    expect(reading("Owned since")).toHaveTextContent("2d ago");
    expect(reading("Last updated")).toHaveTextContent("3h ago");
    expect(reading("Claim link")).toHaveTextContent("None");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reassign owner" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Unassign" })).toBeEnabled();

    fireEvent.click(owner);

    expect(screen.getByText("Account page")).toBeInTheDocument();
  });

  it("an owner id with no account behind it reads Not an account, with the id and no link", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(
      claim({ status: "owned", owner: { user_id: "admin-identity", email: null, name: null }, owned_since: NOW - DAY }),
    );
    renderCard();
    await loaded("Owned");

    expect(reading("Owner")).toHaveTextContent("Not an account");
    expect(reading("Owner")).toHaveTextContent("admin-identity");
    expect(within(reading("Owner")).queryByRole("link")).not.toBeInTheDocument();
    // An owner assigned rather than claimed has no address on file.
    expect(reading("Claim address")).toHaveTextContent("—");
    expect(reading("Verified")).toHaveTextContent("—");
  });

  it("an address on file reads Yes or No for verified, and no address reads a dash", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(claim({ status: "owned", owner: ADA_OWNER, email: ADA, verified: false }));
    renderCard();
    await loaded("Owned");

    expect(reading("Verified")).toHaveTextContent("No");
  });

  it("a live claim link reads pending, with where it went and when it runs out", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(
      claim({
        status: "pending",
        email: ADA,
        updated_at: NOW - 2 * HOUR,
        challenge: { email: ADA, created_at: NOW - 2 * HOUR, expires_at: NOW + 22 * HOUR, expired: false },
      }),
    );
    renderCard();
    await loaded("Pending");

    expect(reading("Owner")).toHaveTextContent("Nobody");
    expect(reading("Claim address")).toHaveTextContent(ADA);
    expect(reading("Verified")).toHaveTextContent("No");
    expect(reading("Claim link")).toHaveTextContent(`${ADA}Sent 2h ago, expires in 22h`);
    expect(within(reading("Claim link")).getByText(/^Sent/)).not.toHaveClass("admin-claim-expired");
    // Nobody to take it from.
    expect(screen.getByRole("button", { name: "Assign owner" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Unassign" })).not.toBeInTheDocument();
  });

  it("a claim link that ran out is marked expired", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(
      claim({
        email: ADA,
        challenge: { email: ADA, created_at: NOW - 3 * DAY, expires_at: NOW - 2 * DAY, expired: true },
      }),
    );
    renderCard();
    await loaded("Link expired");

    const when = within(reading("Claim link")).getByText("Sent 3d ago, expired 2d ago");
    expect(when).toHaveClass("admin-claim-expired");
  });

  it("goes by the clock for a link that ran out after the server read it", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(
      claim({
        status: "pending",
        email: ADA,
        challenge: { email: ADA, created_at: NOW - DAY, expires_at: NOW - 60, expired: false },
      }),
    );
    renderCard();
    await loaded("Link expired");

    expect(within(reading("Claim link")).getByText(/^Sent/)).toHaveClass("admin-claim-expired");
  });

  it("a bounced address is a warning in plain words", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(
      claim({ email: ADA, undeliverable: true, updated_at: NOW - HOUR }),
    );
    renderCard();
    await loaded("Mail bounced");

    expect(screen.getByRole("alert")).toHaveTextContent(
      `Mail to ${ADA} bounced. The node is told so, and asks its owner for another address.`,
    );
    expect(reading("Claim address")).toHaveTextContent(ADA);
    expect(reading("Claim link")).toHaveTextContent("None");
  });

  it("a node nobody has claimed has nothing on file", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(claim());
    renderCard();
    await loaded("Unclaimed");

    expect(reading("Owner")).toHaveTextContent("Nobody");
    expect(reading("Claim address")).toHaveTextContent("—");
    expect(reading("Verified")).toHaveTextContent("—");
    expect(reading("Owned since")).toHaveTextContent("—");
    expect(reading("Last updated")).toHaveTextContent("—");
    expect(reading("Claim link")).toHaveTextContent("None");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says it is loading until the claim is in", async () => {
    const answer = deferred<AdminClaim>();
    vi.mocked(api.adminNodeClaim).mockReturnValue(answer.promise);
    renderCard();

    expect(within(await screen.findByRole("region", { name: "Claim" })).getByText("Loading…")).toBeInTheDocument();

    await act(async () => answer.resolve(claim()));
    await loaded("Unclaimed");
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });
});

describe("a node with no claim to read", () => {
  const UNREGISTERED = "This node has not registered with the server, so it has no claim.";

  it("a name the node map does not hold never registered, and its claim is not asked for", async () => {
    vi.mocked(api.adminNodeRefs).mockResolvedValue({ "other-ref": "ret00000000" });
    renderCard();

    expect(await screen.findByText(UNREGISTERED)).toBeInTheDocument();
    expect(api.adminNodeClaim).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /assign/i })).not.toBeInTheDocument();
  });

  it("checks again on request, for a node that has registered since", async () => {
    vi.mocked(api.adminNodeRefs).mockResolvedValueOnce({});
    vi.mocked(api.adminNodeClaim).mockResolvedValue(claim());
    renderCard();
    await screen.findByText(UNREGISTERED);

    fireEvent.click(screen.getByRole("button", { name: "Check again" }));

    await loaded("Unclaimed");
    expect(api.adminNodeRefs).toHaveBeenCalledTimes(2);
    expect(api.adminNodeClaim).toHaveBeenCalledWith(ID);
  });

  it("a node the map holds but the server has no registration for reads the same", async () => {
    // A synthetic node connected over TCP is in the map under its own id.
    vi.mocked(api.adminNodeRefs).mockResolvedValue({ "synth-gvl-001": "synth-gvl-001" });
    vi.mocked(api.adminNodeClaim).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderCard("synth-gvl-001");

    expect(await screen.findByText(UNREGISTERED)).toBeInTheDocument();
    expect(api.adminNodeClaim).toHaveBeenCalledWith("synth-gvl-001");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["constructor", "__proto__", "toString"])("a name that is a key of every object, %s, finds nothing", async (name) => {
    renderCard(name);

    expect(await screen.findByText(UNREGISTERED)).toBeInTheDocument();
    expect(api.adminNodeClaim).not.toHaveBeenCalled();
  });

  it("a failed read of the map is an error with a retry, not an absence", async () => {
    vi.mocked(api.adminNodeRefs).mockRejectedValueOnce(new HttpError(503, "Service Unavailable", null));
    vi.mocked(api.adminNodeClaim).mockResolvedValue(claim());
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderCard();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the claim: 503 Service Unavailable");
    expect(screen.queryByText(UNREGISTERED)).not.toBeInTheDocument();
    expect(api.adminNodeClaim).not.toHaveBeenCalled();

    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));

    await loaded("Unclaimed");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a failed read of the claim is an error, and nothing is offered to change", async () => {
    vi.mocked(api.adminNodeClaim).mockRejectedValue(new HttpError(500, "Internal Server Error", null));
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderCard();

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the claim: 500 Internal Server Error");
    expect(screen.queryByRole("button", { name: /assign/i })).not.toBeInTheDocument();
  });
});

describe("changing the owner from the card", () => {
  it("reassigns: search, pick, confirm, then the card reads the claim again", async () => {
    vi.mocked(api.adminNodeClaim)
      .mockResolvedValueOnce(OWNED_BY_ADA)
      .mockResolvedValueOnce(
        claim({
          status: "owned",
          owner: { user_id: BOB.id, email: BOB.email, name: BOB.name },
          updated_at: NOW,
          owned_since: NOW,
        }),
      );
    vi.mocked(api.adminUsers).mockResolvedValue({ users: [BOB], total: 1 });
    vi.mocked(api.adminSetNodeOwner).mockResolvedValue({ ok: true, node_id: ID, user_id: BOB.id });
    renderCard();
    await loaded("Owned");

    fireEvent.click(screen.getByRole("button", { name: "Reassign owner" }));
    const dialog = screen.getByRole("dialog", { name: "Reassign owner" });
    fireEvent.change(within(dialog).getByLabelText("Search accounts by email"), { target: { value: "bob" } });
    fireEvent.click(await within(dialog).findByRole("radio", { name: /bob@example\.com/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Reassign" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.adminUsers).toHaveBeenCalledWith({ q: "bob", limit: 6 });
    expect(api.adminSetNodeOwner).toHaveBeenCalledWith(ID, BOB.id);
    expect(status()).toHaveTextContent(`Owner changed to ${BOB.email}.`);
    await waitFor(() => expect(within(reading("Owner")).getByRole("link", { name: BOB.email })).toBeInTheDocument());
    expect(api.adminNodeClaim).toHaveBeenCalledTimes(2);
    expect(reading("Claim address")).toHaveTextContent("—");
    expect(screen.getByRole("button", { name: "Reassign owner" })).toHaveFocus();
  });

  it("unassigns after its own confirm, then the card reads the claim again", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValueOnce(OWNED_BY_ADA).mockResolvedValueOnce(claim());
    vi.mocked(api.adminSetNodeOwner).mockResolvedValue({ ok: true, node_id: ID, user_id: null });
    renderCard();
    await loaded("Owned");

    fireEvent.click(screen.getByRole("button", { name: "Unassign" }));
    const dialog = screen.getByRole("dialog", { name: "Unassign the owner?" });
    expect(dialog).toHaveTextContent(`${REF} is taken from ${ADA} and left with no owner.`);
    fireEvent.click(within(dialog).getByRole("button", { name: "Unassign" }));

    await loaded("Unclaimed");
    expect(api.adminSetNodeOwner).toHaveBeenCalledWith(ID, null);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(status()).toHaveTextContent("Owner removed.");
    expect(reading("Owner")).toHaveTextContent("Nobody");
    // Its own button went with the owner, so focus moves to the one left.
    expect(screen.getByRole("button", { name: "Assign owner" })).toHaveFocus();
  });

  it("Escape closes the dialog, changes nothing and gives focus back", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(OWNED_BY_ADA);
    renderCard();
    await loaded("Owned");
    const unassign = screen.getByRole("button", { name: "Unassign" });
    unassign.focus();

    fireEvent.click(unassign);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.adminSetNodeOwner).not.toHaveBeenCalled();
    expect(api.adminNodeClaim).toHaveBeenCalledTimes(1);
    expect(unassign).toHaveFocus();
  });

  it("a polled radar's refusal is said in the dialog, which stays open", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(OWNED_BY_ADA);
    vi.mocked(api.adminSetNodeOwner).mockRejectedValue(
      new HttpError(409, "Conflict", { detail: `${ID} is a polled radar: reassign it, or have its owner remove it` }),
    );
    renderCard();
    await loaded("Owned");

    fireEvent.click(screen.getByRole("button", { name: "Unassign" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Unassign" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Not unassigned: a polled radar keeps an owner while it is polled. Reassign it to another account, or have its owner remove it.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(api.adminNodeClaim).toHaveBeenCalledTimes(1);
    expect(status()).toBeEmptyDOMElement();
  });

  it("a node gone from the registry is said in the dialog while the card reads it again", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(OWNED_BY_ADA);
    renderCard();
    await loaded("Owned");
    vi.mocked(api.adminNodeRefs).mockResolvedValue({});
    vi.mocked(api.adminSetNodeOwner).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));

    fireEvent.click(screen.getByRole("button", { name: "Unassign" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Unassign" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "This node is no longer registered, so it cannot have an owner.",
    );
    expect(await screen.findByText("This node has not registered with the server, so it has no claim.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("gives focus to Check again when the node turned out unregistered under the dialog", async () => {
    vi.mocked(api.adminNodeClaim).mockResolvedValue(OWNED_BY_ADA);
    renderCard();
    await loaded("Owned");
    vi.mocked(api.adminNodeRefs).mockResolvedValue({});
    vi.mocked(api.adminUsers).mockResolvedValue({ users: [BOB], total: 1 });
    vi.mocked(api.adminSetNodeOwner).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));

    fireEvent.click(screen.getByRole("button", { name: "Reassign owner" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Search accounts by email"), { target: { value: "bob" } });
    fireEvent.click(await within(dialog).findByRole("radio", { name: /bob@example\.com/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Reassign" }));
    await within(dialog).findByRole("alert");
    await screen.findByText("This node has not registered with the server, so it has no claim.");
    expect(screen.queryByRole("button", { name: "Reassign owner" })).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check again" })).toHaveFocus();
  });
});
