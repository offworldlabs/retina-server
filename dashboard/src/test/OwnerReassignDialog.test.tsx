import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: {
    adminUsers: vi.fn(),
    adminSetNodeOwner: vi.fn(),
  },
}));

import { api } from "../api/client";
import { OwnerReassignDialog, type OwnerDialogMode } from "../pages/admin/claims/OwnerReassignDialog";
import type { AdminClaim, AdminUser, AdminUserPage } from "../types";

const ID = "ret0a1b2c3d";
const REF = "nde0a1b2c3d4e5f";

function account(overrides: Partial<AdminUser>): AdminUser {
  return {
    id: "00000000-0000-4000-8000-000000000000",
    email: "someone@example.com",
    name: "",
    role: "user",
    is_superuser: false,
    is_active: true,
    created_at: 1_780_000_000,
    last_seen_at: null,
    is_verified: true,
    node_count: 0,
    ...overrides,
  };
}

const ADA = account({ id: "6f0e1d2c-3b4a-4958-8776-655443322110", email: "ada@example.com", name: "Ada", node_count: 1 });
const BOB = account({ id: "0a1b2c3d-4e5f-4a6b-8c7d-8e9fa0b1c2d3", email: "bob@example.com", name: "Bob" });
const CAROL = account({ id: "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e", email: "carol@example.com", is_active: false });

const ADA_OWNER: AdminClaim["owner"] = { user_id: ADA.id, email: ADA.email, name: ADA.name };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function open({ mode = "reassign", owner = ADA_OWNER }: { mode?: OwnerDialogMode; owner?: AdminClaim["owner"] } = {}) {
  const onClose = vi.fn();
  const onChanged = vi.fn();
  render(
    <OwnerReassignDialog mode={mode} nodeId={ID} nodeName={REF} owner={owner} onClose={onClose} onChanged={onChanged} />,
  );
  return { onClose, onChanged, dialog: screen.getByRole("dialog") };
}

const search = () => screen.getByLabelText("Search accounts by email");
const type = (value: string) => fireEvent.change(search(), { target: { value } });
const note = () => within(screen.getByRole("dialog")).getByRole("status");
const submit = (name: string) => screen.getByRole("button", { name });

/** Types `q`, waits out the search and returns the radios it found. */
async function find(q: string) {
  type(q);
  await screen.findByRole("radiogroup", { name: "Matching accounts" });
  return screen.getAllByRole("radio");
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.adminUsers).mockResolvedValue({ users: [BOB, CAROL, ADA], total: 3 });
});

describe("finding the account", () => {
  it("starts in the search field and asks for part of an address", () => {
    open();

    expect(screen.getByRole("dialog", { name: "Reassign owner" })).toHaveAccessibleDescription(
      `Pick the account that should own ${REF}.`,
    );
    expect(search()).toHaveFocus();
    expect(note()).toHaveTextContent("Type part of their address.");
    expect(submit("Reassign")).toBeDisabled();
    expect(api.adminUsers).not.toHaveBeenCalled();
  });

  it("searches once typing pauses, for a handful of accounts", async () => {
    open();

    type("b");
    type("bo");
    type(" bob ");

    expect(note()).toHaveTextContent("Searching…");
    expect(api.adminUsers).not.toHaveBeenCalled();
    await waitFor(() => expect(api.adminUsers).toHaveBeenCalledTimes(1));
    expect(api.adminUsers).toHaveBeenCalledWith({ q: "bob", limit: 6 });
    expect(await screen.findByRole("radiogroup")).toBeInTheDocument();
    expect(note()).toHaveTextContent("3 accounts found.");
  });

  it("shows each account's address, name and whether it can sign in, as one radio group", async () => {
    open();

    const radios = await find("example");

    expect(radios).toHaveLength(3);
    expect(new Set(radios.map((radio) => radio.getAttribute("name"))).size).toBe(1);
    const [bob, carol, ada] = radios.map((radio) => radio.closest("label") as HTMLElement);
    expect(bob).toHaveTextContent("bob@example.comBobActive");
    expect(carol).toHaveTextContent("carol@example.comDeactivated");
    // The owner already: shown, and not a choice.
    expect(ada).toHaveTextContent("ada@example.comAdaCurrent ownerActive");
    expect(within(ada).getByRole("radio")).toBeDisabled();
    expect(within(bob).getByRole("radio")).toBeEnabled();
  });

  it("says when there are more matches than it shows", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue({
      users: Array.from({ length: 6 }, (_, i) => account({ id: `id-${i}`, email: `user${i}@example.com` })),
      total: 23,
    });
    open();

    await find("user");

    expect(note()).toHaveTextContent("6 of 23 shown. Type more of the address to narrow them.");
  });

  it("says when nothing matches", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue({ users: [], total: 0 });
    open();

    type("zzz");

    await waitFor(() => expect(note()).toHaveTextContent("No account matches “zzz”."));
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });

  it("says when the search failed, and searches again on Retry", async () => {
    vi.mocked(api.adminUsers).mockRejectedValueOnce(new HttpError(503, "Service Unavailable", null));
    open();

    type("bob");

    await waitFor(() => expect(note()).toHaveTextContent("Could not search: 503 Service Unavailable."));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(note()).toHaveTextContent("Searching…");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    await screen.findByRole("radiogroup");
    expect(api.adminUsers).toHaveBeenCalledTimes(2);
    expect(note()).toHaveTextContent("3 accounts found.");
  });

  it("shows the answer to what is typed now, whichever answer lands last", async () => {
    const first = deferred<AdminUserPage>();
    const second = deferred<AdminUserPage>();
    vi.mocked(api.adminUsers).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    open();

    type("bo");
    await waitFor(() => expect(api.adminUsers).toHaveBeenCalledTimes(1));
    type("bob");
    await waitFor(() => expect(api.adminUsers).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve({ users: [BOB], total: 1 }));
    await act(async () => first.resolve({ users: [CAROL], total: 1 }));

    expect(screen.getAllByRole("radio")).toHaveLength(1);
    expect(screen.getByRole("radio", { name: /bob@example\.com/ })).toBeInTheDocument();
  });
});

describe("confirming", () => {
  it("names the node, the new owner and the current one, and says what resets", async () => {
    open();
    await find("bob");

    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));

    const confirm = `${REF} moves from ${ADA.email} to ${BOB.email}. Its claim address and verification are reset, and a claim link still out stops working.`;
    expect(screen.getByText(/moves from/)).toHaveTextContent(confirm);
    expect(submit("Reassign")).toBeEnabled();
    expect(submit("Reassign")).toHaveAccessibleDescription(confirm);
  });

  it("calls the owner route with the account picked, and reports the change", async () => {
    vi.mocked(api.adminSetNodeOwner).mockResolvedValue({ ok: true, node_id: ID, user_id: BOB.id });
    const { onChanged, onClose } = open();
    await find("bob");
    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));

    fireEvent.click(submit("Reassign"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "reassigned", email: BOB.email }));
    expect(api.adminSetNodeOwner).toHaveBeenCalledWith(ID, BOB.id);
    expect(api.adminSetNodeOwner).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not confirm on Enter in the search field, where it means search", async () => {
    open();
    await find("bob");
    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));

    const notPrevented = fireEvent.keyDown(search(), { key: "Enter" });

    expect(notPrevented).toBe(false);
    expect(api.adminSetNodeOwner).not.toHaveBeenCalled();
  });

  it("forgets a pick the latest search no longer lists", async () => {
    open();
    await find("example");
    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));
    vi.mocked(api.adminUsers).mockResolvedValue({ users: [CAROL], total: 1 });

    type("carol");

    await waitFor(() => expect(screen.getAllByRole("radio")).toHaveLength(1));
    expect(screen.queryByText(/moves from/)).not.toBeInTheDocument();
    expect(submit("Reassign")).toBeDisabled();
  });

  it("says a deactivated account cannot sign in to see the node", async () => {
    open();
    await find("carol");

    fireEvent.click(screen.getByRole("radio", { name: /carol@example\.com/ }));

    expect(screen.getByText(/moves from/)).toHaveTextContent("This account is deactivated, so they cannot sign in to see it.");
  });

  it("assigns, rather than reassigns, a node with no owner", async () => {
    vi.mocked(api.adminSetNodeOwner).mockResolvedValue({ ok: true, node_id: ID, user_id: BOB.id });
    const { onChanged } = open({ owner: null });
    expect(screen.getByRole("dialog", { name: "Assign owner" })).toBeInTheDocument();
    await find("bob");

    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));

    expect(screen.getByText(/goes to/)).toHaveTextContent(`${REF} has no owner now and goes to ${BOB.email}.`);
    fireEvent.click(submit("Assign"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "reassigned", email: BOB.email }));
  });

  it("names an owner with no account as such", async () => {
    open({ owner: { user_id: "admin-identity", email: null, name: null } });
    await find("bob");

    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));

    expect(screen.getByText(/moves from/)).toHaveTextContent(`${REF} moves from an owner with no account to ${BOB.email}.`);
  });
});

describe("refusals", () => {
  async function pickBobAndConfirm(failure: unknown) {
    vi.mocked(api.adminSetNodeOwner).mockRejectedValue(failure);
    const handlers = open();
    await find("bob");
    fireEvent.click(screen.getByRole("radio", { name: /bob@example\.com/ }));
    fireEvent.click(submit("Reassign"));
    return { ...handlers, alert: await within(handlers.dialog).findByRole("alert") };
  }

  it("an account gone since the search: says so and searches again", async () => {
    const { alert, onChanged } = await pickBobAndConfirm(
      new HttpError(404, "Not Found", { detail: "User not found" }),
    );

    expect(alert).toHaveTextContent("That account no longer exists. Pick another.");
    expect(onChanged).not.toHaveBeenCalled();
    expect(submit("Reassign")).toBeDisabled();
    await waitFor(() => expect(api.adminUsers).toHaveBeenCalledTimes(2));
  });

  it("a node gone from the registry: says so, and tells the card", async () => {
    const { alert, onChanged } = await pickBobAndConfirm(
      new HttpError(404, "Not Found", { detail: "Node not found" }),
    );

    expect(alert).toHaveTextContent("This node is no longer registered, so it cannot have an owner.");
    expect(onChanged).toHaveBeenCalledWith({ kind: "gone" });
    expect(submit("Reassign")).toBeDisabled();
  });

  it("anything else: names what went wrong", async () => {
    const { alert, onChanged } = await pickBobAndConfirm(new Error("Request timed out"));

    expect(alert).toHaveTextContent("Not reassigned: Request timed out.");
    expect(onChanged).not.toHaveBeenCalled();
    expect(submit("Reassign")).toBeEnabled();
  });
});

describe("unassigning", () => {
  it("has a confirm of its own, and sends no account", async () => {
    vi.mocked(api.adminSetNodeOwner).mockResolvedValue({ ok: true, node_id: ID, user_id: null });
    const { onChanged, dialog } = open({ mode: "unassign" });

    expect(dialog).toHaveAccessibleName("Unassign the owner?");
    expect(dialog).toHaveAccessibleDescription(
      `${REF} is taken from ${ADA.email} and left with no owner. Its claim address and verification are reset, and it can be claimed again.`,
    );
    expect(screen.queryByLabelText("Search accounts by email")).not.toBeInTheDocument();
    // Nothing to type, so the button that changes nothing has focus.
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    fireEvent.click(submit("Unassign"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "unassigned" }));
    expect(api.adminSetNodeOwner).toHaveBeenCalledWith(ID, null);
    expect(api.adminUsers).not.toHaveBeenCalled();
  });

  it("a polled radar's 409 is said in plain words, and the dialog stays", async () => {
    vi.mocked(api.adminSetNodeOwner).mockRejectedValue(
      new HttpError(409, "Conflict", { detail: `${ID} is a polled radar: reassign it, or have its owner remove it` }),
    );
    const { onChanged, onClose, dialog } = open({ mode: "unassign" });

    fireEvent.click(submit("Unassign"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Not unassigned: a polled radar keeps an owner while it is polled. Reassign it to another account, or have its owner remove it.",
    );
    expect(onChanged).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(submit("Unassign")).toBeEnabled();
  });
});

describe("closing", () => {
  it("closes on Escape", () => {
    const { onClose, dialog } = open();

    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(api.adminSetNodeOwner).not.toHaveBeenCalled();
  });

  it("closes on Cancel", () => {
    const { onClose } = open({ mode: "unassign" });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("stays open while the change is in flight, and says it is working", async () => {
    const answer = deferred<{ ok: true; node_id: string; user_id: string | null }>();
    vi.mocked(api.adminSetNodeOwner).mockReturnValue(answer.promise);
    const { onClose, onChanged, dialog } = open({ mode: "unassign" });

    fireEvent.click(submit("Unassign"));
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(submit("Unassigning…")).toBeDisabled();

    fireEvent.click(submit("Unassigning…"));
    await act(async () => answer.resolve({ ok: true, node_id: ID, user_id: null }));

    expect(api.adminSetNodeOwner).toHaveBeenCalledTimes(1);
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});
