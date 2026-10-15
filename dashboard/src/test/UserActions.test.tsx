import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: {
    adminDeactivateUser: vi.fn(),
    adminReactivateUser: vi.fn(),
    adminSendSignInLink: vi.fn(),
    adminDeleteUser: vi.fn(),
    adminChangeUserEmail: vi.fn(),
  },
}));

import { api } from "../api/client";
import { ChangeEmailDialog } from "../pages/admin/users/ChangeEmailDialog";
import { UserActions } from "../pages/admin/users/UserActions";
import type { AdminAccount, AdminUser } from "../types";

/** What deactivate and reactivate answer with. */
const ACCOUNT: AdminAccount = {
  id: "u1",
  email: "ada@example.com",
  name: "Ada",
  role: "user",
  is_superuser: false,
  is_active: true,
  created_at: 1_780_000_000,
  last_seen_at: null,
};

const ADA: AdminUser = { ...ACCOUNT, is_verified: true, node_count: 2 };

const button = (name: string | RegExp) => screen.getByRole("button", { name });
const status = () => screen.getByRole("status");

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("deactivating and reactivating", () => {
  it("asks first, saying it ends every session, then deactivates and reports it", async () => {
    vi.mocked(api.adminDeactivateUser).mockResolvedValue({ ...ACCOUNT, is_active: false });
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Deactivate"));

    const confirm = screen.getByRole("group", { name: "Deactivate? It ends every session they have." });
    expect(button("Keep active")).toHaveFocus();
    expect(api.adminDeactivateUser).not.toHaveBeenCalled();

    fireEvent.click(within(confirm).getByRole("button", { name: "Deactivate" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "deactivated" }));
    expect(api.adminDeactivateUser).toHaveBeenCalledWith("u1");
    expect(status()).toHaveTextContent("Deactivated. All their sessions have ended.");
    // The server's answer shows before the caller's refetch lands.
    expect(button("Reactivate")).toHaveFocus();
  });

  it("changes nothing when the confirm is declined", () => {
    render(<UserActions user={ADA} onChanged={vi.fn()} />);

    fireEvent.click(button("Deactivate"));
    fireEvent.click(button("Keep active"));

    expect(api.adminDeactivateUser).not.toHaveBeenCalled();
    expect(button("Deactivate")).toHaveFocus();
  });

  it("keeps the confirm up and says why when deactivating fails", async () => {
    vi.mocked(api.adminDeactivateUser).mockRejectedValue(new HttpError(500, "Internal Server Error", null));
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Deactivate"));
    fireEvent.click(within(screen.getByRole("group", { name: /^Deactivate\?/ })).getByRole("button", { name: "Deactivate" }));

    await waitFor(() => expect(status()).toHaveTextContent("Could not deactivate: 500 Internal Server Error."));
    expect(status()).toHaveClass("error");
    expect(button("Keep active")).toBeEnabled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("reactivates a deactivated account at once", async () => {
    vi.mocked(api.adminReactivateUser).mockResolvedValue({ ...ACCOUNT, is_active: true });
    const onChanged = vi.fn();
    render(<UserActions user={{ ...ADA, is_active: false }} onChanged={onChanged} />);

    fireEvent.click(button("Reactivate"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "reactivated" }));
    expect(api.adminReactivateUser).toHaveBeenCalledWith("u1");
    expect(status()).toHaveTextContent("Reactivated. They sign in again with a new link.");
    expect(button("Deactivate")).toHaveFocus();
  });

  it("follows a refetched row over its own last answer", async () => {
    vi.mocked(api.adminDeactivateUser).mockResolvedValue({ ...ACCOUNT, is_active: false });
    const { rerender } = render(<UserActions user={ADA} onChanged={vi.fn()} />);
    fireEvent.click(button("Deactivate"));
    fireEvent.click(within(screen.getByRole("group", { name: /^Deactivate\?/ })).getByRole("button", { name: "Deactivate" }));
    await screen.findByRole("button", { name: "Reactivate" });

    // Reactivated elsewhere since: the fresh row is what counts.
    rerender(<UserActions user={{ ...ADA }} onChanged={vi.fn()} />);

    expect(button("Deactivate")).toBeInTheDocument();
  });
});

describe("sending a sign-in link", () => {
  it("says the link is queued, in a polite live region", async () => {
    vi.mocked(api.adminSendSignInLink).mockResolvedValue({ status: "queued", email: "ada@example.com" });
    render(<UserActions user={ADA} onChanged={vi.fn()} />);

    fireEvent.click(button("Send sign-in link"));

    await waitFor(() => expect(status()).toHaveTextContent("Sign-in link queued for ada@example.com."));
    expect(api.adminSendSignInLink).toHaveBeenCalledWith("u1");
    expect(status()).not.toHaveClass("error");
    expect(button("Send sign-in link")).toHaveFocus();
  });

  it.each([
    [409, "Conflict", "Account is deactivated"],
    [429, "Too Many Requests", "This address already holds the maximum number of outstanding sign-in links"],
    [503, "Service Unavailable", "Mail cannot leave this server: no mail provider is configured"],
  ])("repeats a %i refusal in the server's own words", async (code, statusText, detail) => {
    vi.mocked(api.adminSendSignInLink).mockRejectedValue(new HttpError(code, statusText, { detail }));
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Send sign-in link"));

    await waitFor(() => expect(status()).toHaveTextContent(`Not sent: ${detail}.`));
    expect(status()).toHaveClass("error");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("disables every action while the request is in flight", async () => {
    let answer: (value: { status: "queued"; email: string }) => void = () => {};
    vi.mocked(api.adminSendSignInLink).mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    render(<UserActions user={ADA} onChanged={vi.fn()} />);

    fireEvent.click(button("Send sign-in link"));

    for (const name of ["Deactivate", "Sending…", "Change email", "Delete"]) expect(button(name)).toBeDisabled();

    answer({ status: "queued", email: "ada@example.com" });
    await waitFor(() => expect(button("Send sign-in link")).toBeEnabled());
    for (const name of ["Deactivate", "Change email", "Delete"]) expect(button(name)).toBeEnabled();
  });

  it("tells the caller when the account has gone", async () => {
    vi.mocked(api.adminSendSignInLink).mockRejectedValue(new HttpError(404, "Not Found", { detail: "User not found" }));
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Send sign-in link"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "gone" }));
    expect(status()).toHaveTextContent("This account no longer exists.");
  });
});

describe("deleting", () => {
  function openDelete() {
    render(<UserActions user={ADA} onChanged={onChanged} />);
    fireEvent.click(button("Delete"));
    return screen.getByRole("dialog", { name: "Delete this account?" });
  }
  const onChanged = vi.fn();

  it("says what deleting does, and waits for the exact address", () => {
    const dialog = openDelete();
    const field = within(dialog).getByRole("textbox", { name: "Type ada@example.com to confirm" });
    const confirm = within(dialog).getByRole("button", { name: "Delete account" });

    expect(dialog).toHaveAccessibleDescription(
      "ada@example.com is removed for good. Their 2 nodes are released; any polled radar they connected is removed. Every session they hold ends.",
    );
    expect(field).toHaveFocus();
    expect(confirm).toBeDisabled();

    for (const near of ["ada@example", "ADA@example.com", "ada@example.co", "bob@example.com"]) {
      fireEvent.change(field, { target: { value: near } });
      expect(confirm).toBeDisabled();
    }

    fireEvent.change(field, { target: { value: "ada@example.com" } });
    expect(confirm).toBeEnabled();
  });

  it.each([
    [1, "Their 1 node is released, or removed if it is a polled radar they connected."],
    [0, "They own no nodes."],
  ])("says what happens to the nodes of an account that owns %i", (nodes, said) => {
    render(<UserActions user={{ ...ADA, node_count: nodes }} onChanged={vi.fn()} />);
    fireEvent.click(button("Delete"));

    expect(screen.getByRole("dialog", { name: "Delete this account?" })).toHaveAccessibleDescription(
      `ada@example.com is removed for good. ${said} Every session they hold ends.`,
    );
  });

  it("deletes once the address is typed, and reports how many nodes were released", async () => {
    vi.mocked(api.adminDeleteUser).mockResolvedValue({ ok: true, released: 2 });
    const dialog = openDelete();

    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "deleted", released: 2 }));
    expect(api.adminDeleteUser).toHaveBeenCalledWith("u1");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says a node was claimed meanwhile, keeps the account, and a second press finishes", async () => {
    const claimedMeanwhile = "A node was claimed for this account while it was being deleted. Try again.";
    vi.mocked(api.adminDeleteUser)
      .mockRejectedValueOnce(new HttpError(409, "Conflict", { detail: claimedMeanwhile }))
      .mockResolvedValueOnce({ ok: true, released: 3 });
    const dialog = openDelete();
    const field = within(dialog).getByRole("textbox");

    fireEvent.change(field, { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(`Not deleted: ${claimedMeanwhile}`);
    // Still there to delete: the typed address stands and the press is live again.
    expect(field).toHaveValue("ada@example.com");
    expect(within(dialog).getByRole("button", { name: "Delete account" })).toBeEnabled();
    expect(onChanged).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "deleted", released: 3 }));
    expect(api.adminDeleteUser).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the server's refusal in the dialog and lets it be tried again", async () => {
    vi.mocked(api.adminDeleteUser).mockRejectedValue(
      new HttpError(500, "Internal Server Error", { detail: "database is locked" }),
    );
    const dialog = openDelete();

    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Not deleted: database is locked.");
    expect(within(dialog).getByRole("button", { name: "Delete account" })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("cannot be cancelled while the delete is in flight", async () => {
    vi.mocked(api.adminDeleteUser).mockReturnValue(new Promise(() => {}));
    const dialog = openDelete();

    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    expect(within(dialog).getByRole("button", { name: "Deleting…" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("closes on Cancel without deleting, focus back on Delete", () => {
    const dialog = openDelete();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.adminDeleteUser).not.toHaveBeenCalled();
    expect(button("Delete")).toHaveFocus();
  });
});

describe("changing the address", () => {
  const field = () => screen.getByRole("textbox", { name: "New address" });

  it("is offered beside the other actions", () => {
    render(<UserActions user={ADA} onChanged={vi.fn()} />);

    expect(button("Change email")).toBeEnabled();
  });

  it("says up front that a link goes to the new address and nothing changes until it is opened", () => {
    render(<UserActions user={ADA} onChanged={vi.fn()} />);

    fireEvent.click(button("Change email"));

    expect(screen.getByRole("dialog", { name: "Change email address" })).toHaveAccessibleDescription(
      "A confirmation link goes to the new address. Until it is opened, the account keeps signing in at ada@example.com.",
    );
  });

  it("sends the link from the dialog on its own, with the address the server settled on", async () => {
    vi.mocked(api.adminChangeUserEmail).mockResolvedValue({ status: "sent", email: "ada@new.example" });
    const onChanged = vi.fn();
    render(<ChangeEmailDialog user={ADA} onClose={vi.fn()} onChanged={onChanged} />);
    const confirm = button("Send link");

    expect(field()).toHaveFocus();
    expect(confirm).toBeDisabled();
    for (const unusable of ["ada", "ada@", "ADA@example.com", "two words@example.com"]) {
      fireEvent.change(field(), { target: { value: unusable } });
      expect(confirm).toBeDisabled();
    }

    fireEvent.change(field(), { target: { value: "  Ada@New.Example " } });
    fireEvent.click(confirm);

    expect(button("Sending…")).toBeDisabled();
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "email-sent", email: "ada@new.example" }));
    expect(api.adminChangeUserEmail).toHaveBeenCalledWith("u1", "Ada@New.Example");
  });

  it("says in the row that the link went out and the address changes once it is opened", async () => {
    vi.mocked(api.adminChangeUserEmail).mockResolvedValue({ status: "sent", email: "ada@new.example" });
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Change email"));
    fireEvent.change(field(), { target: { value: "ada@new.example" } });
    fireEvent.click(button("Send link"));

    await waitFor(() =>
      expect(status()).toHaveTextContent(
        "Confirmation link sent to ada@new.example. The address changes once it is opened.",
      ),
    );
    expect(status()).not.toHaveClass("error");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onChanged).toHaveBeenCalledWith({ kind: "email-sent", email: "ada@new.example" });
    expect(button("Change email")).toHaveFocus();
  });

  it.each([
    [400, "Bad Request", "That is not an email address"],
    [409, "Conflict", "That address belongs to another account"],
    [409, "Conflict", "Account is deactivated"],
    [429, "Too Many Requests", "Too many address changes have been asked for; try again later"],
    [503, "Service Unavailable", "Changing the address by email is unavailable"],
  ])("shows a %i refusal in the dialog in the server's own words", async (code, statusText, detail) => {
    vi.mocked(api.adminChangeUserEmail).mockRejectedValue(new HttpError(code, statusText, { detail }));
    const onChanged = vi.fn();
    render(<ChangeEmailDialog user={ADA} onClose={vi.fn()} onChanged={onChanged} />);

    fireEvent.change(field(), { target: { value: "bob@example.com" } });
    fireEvent.click(button("Send link"));

    expect(await screen.findByRole("alert")).toHaveTextContent(`Not sent: ${detail}.`);
    expect(button("Send link")).toBeEnabled();
    expect(button("Cancel")).toBeEnabled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("tells the caller when the account has gone", async () => {
    vi.mocked(api.adminChangeUserEmail).mockRejectedValue(new HttpError(404, "Not Found", { detail: "User not found" }));
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Change email"));
    fireEvent.change(field(), { target: { value: "ada@new.example" } });
    fireEvent.click(button("Send link"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "gone" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(status()).toHaveTextContent("This account no longer exists.");
  });
});

describe("starting an action", () => {
  it("is told to the caller before any answer, for each action", () => {
    vi.mocked(api.adminSendSignInLink).mockReturnValue(new Promise(() => {}));
    const onAction = vi.fn();
    const { unmount } = render(<UserActions user={ADA} onChanged={vi.fn()} onAction={onAction} />);

    fireEvent.click(button("Deactivate"));
    expect(onAction).toHaveBeenCalledTimes(1);
    fireEvent.click(button("Keep active"));
    fireEvent.click(button("Change email"));
    expect(onAction).toHaveBeenCalledTimes(2);
    fireEvent.click(button("Cancel"));
    fireEvent.click(button("Delete"));
    expect(onAction).toHaveBeenCalledTimes(3);
    fireEvent.click(button("Cancel"));
    fireEvent.click(button("Send sign-in link"));
    expect(onAction).toHaveBeenCalledTimes(4);
    unmount();

    render(<UserActions user={{ ...ADA, is_active: false }} onChanged={vi.fn()} onAction={onAction} />);
    vi.mocked(api.adminReactivateUser).mockReturnValue(new Promise(() => {}));
    fireEvent.click(button("Reactivate"));
    expect(onAction).toHaveBeenCalledTimes(5);
  });
});
