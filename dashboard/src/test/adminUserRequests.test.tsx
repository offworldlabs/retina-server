import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UserActions } from "../pages/admin/users/UserActions";
import type { AdminUser } from "../types";

// A row's actions over the real API client, with fetch standing in for the
// server: what is asserted here is the request that leaves the console, by
// method, path and body, and what the console makes of the server's answer.

const ADA: AdminUser = {
  id: "8c7b1a2e-0f4d-4c5b-9a6e-1d2c3b4a5f60",
  email: "ada@example.com",
  name: "Ada",
  role: "user",
  is_superuser: false,
  is_active: true,
  created_at: 1_780_000_000,
  last_seen_at: null,
  is_verified: true,
  node_count: 2,
};

const CLAIMED_MEANWHILE = "A node was claimed for this account while it was being deleted. Try again.";

function json(body: unknown, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "Content-Type": "application/json" } });
}

/** Every request made, as method, path and parsed body. */
let requests: { route: string; body: unknown }[];
/** The server's answers to each "METHOD path", in turn. */
let answers: Map<string, Response[]>;

const serve = (route: string, ...replies: Response[]) => answers.set(route, replies);
const button = (name: string) => screen.getByRole("button", { name });

beforeEach(() => {
  requests = [];
  answers = new Map();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      const route = `${init?.method ?? "GET"} ${path}`;
      requests.push({ route, body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
      return answers.get(route)?.shift() ?? json({ detail: "Not Found" }, 404, "Not Found");
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("changing the address", () => {
  it("posts the new address to the account's email route and says the confirmation link was sent", async () => {
    serve(`POST /api/admin/users/${ADA.id}/email`, json({ status: "sent", email: "ada@new.example" }, 202, "Accepted"));
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Change email"));
    fireEvent.change(screen.getByRole("textbox", { name: "New address" }), { target: { value: " Ada@New.Example " } });
    fireEvent.click(button("Send link"));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Confirmation link sent to ada@new.example. The address changes once it is opened.",
      ),
    );
    expect(requests).toEqual([
      { route: `POST /api/admin/users/${ADA.id}/email`, body: { email: "Ada@New.Example" } },
    ]);
    expect(onChanged).toHaveBeenCalledWith({ kind: "email-sent", email: "ada@new.example" });
  });

  it("shows the route's refusal in the dialog", async () => {
    serve(
      `POST /api/admin/users/${ADA.id}/email`,
      json({ detail: "That address belongs to another account" }, 409, "Conflict"),
    );
    render(<UserActions user={ADA} onChanged={vi.fn()} />);

    fireEvent.click(button("Change email"));
    fireEvent.change(screen.getByRole("textbox", { name: "New address" }), { target: { value: "grace@example.com" } });
    fireEvent.click(button("Send link"));

    const dialog = screen.getByRole("dialog", { name: "Change email address" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Not sent: That address belongs to another account.",
    );
  });

  // A deactivated account keeps its address until it is reactivated. The row
  // still offers the change, as it offers a sign-in link, and the route says
  // why it will not start one.
  it("says why a deactivated account's address is not changed", async () => {
    serve(`POST /api/admin/users/${ADA.id}/email`, json({ detail: "Account is deactivated" }, 409, "Conflict"));
    const onChanged = vi.fn();
    render(<UserActions user={{ ...ADA, is_active: false }} onChanged={onChanged} />);

    fireEvent.click(button("Change email"));
    fireEvent.change(screen.getByRole("textbox", { name: "New address" }), { target: { value: "ada@new.example" } });
    fireEvent.click(button("Send link"));

    const dialog = screen.getByRole("dialog", { name: "Change email address" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Not sent: Account is deactivated.");
    expect(requests).toEqual([{ route: `POST /api/admin/users/${ADA.id}/email`, body: { email: "ada@new.example" } }]);
    expect(onChanged).not.toHaveBeenCalled();
  });
});

describe("deleting", () => {
  it("surfaces a 409 for a node claimed meanwhile, and the second press deletes", async () => {
    serve(
      `DELETE /api/admin/users/${ADA.id}`,
      json({ detail: CLAIMED_MEANWHILE }, 409, "Conflict"),
      json({ ok: true, released: 3 }),
    );
    const onChanged = vi.fn();
    render(<UserActions user={ADA} onChanged={onChanged} />);

    fireEvent.click(button("Delete"));
    const dialog = screen.getByRole("dialog", { name: "Delete this account?" });
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "ada@example.com" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(`Not deleted: ${CLAIMED_MEANWHILE}`);
    expect(onChanged).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith({ kind: "deleted", released: 3 }));
    expect(requests.map((request) => request.route)).toEqual([
      `DELETE /api/admin/users/${ADA.id}`,
      `DELETE /api/admin/users/${ADA.id}`,
    ]);
  });
});
