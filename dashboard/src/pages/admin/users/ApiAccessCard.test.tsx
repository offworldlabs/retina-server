import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../api/client", () => ({
  api: {
    adminUserApiAccess: vi.fn(),
    adminUserApiKeys: vi.fn(),
    adminGrantApiAccess: vi.fn(),
    adminRevokeApiGrant: vi.fn(),
    adminRevokeApiKey: vi.fn(),
  },
}));

import { api } from "../../../api/client";
import type { AdminUserApiAccess, ApiKeyInfo } from "../../../types";
import { ApiAccessCard } from "./ApiAccessCard";

const mocked = vi.mocked(api);

const NO_ACCESS: AdminUserApiAccess = {
  access: { entitled: false, kind: null, reason: "not_entitled", limits: null, contributor: null, grant_ends_at: null },
  entitlements: [],
};
const GRANTED: AdminUserApiAccess = {
  access: { entitled: true, kind: "customer", reason: null, limits: null, contributor: null, grant_ends_at: null },
  entitlements: [{ id: "e1", kind: "customer", source: "manual", starts_at: "2026-10-30T10:00:00Z", ends_at: null, lapsed_at: null }],
};
const KEY: ApiKeyInfo = { id: "k1", name: "Nightly export", prefix: "ab12cd34", created_at: "2026-10-30T10:00:00Z", last_used_at: null, revoked_at: null };

function show(access: AdminUserApiAccess, keys: ApiKeyInfo[] = []) {
  mocked.adminUserApiAccess.mockResolvedValue(access);
  mocked.adminUserApiKeys.mockResolvedValue(keys);
  render(<ApiAccessCard userId="u1" />);
}

async function openGrantDialog() {
  fireEvent.click(await screen.findByRole("button", { name: "Grant access" }));
  return screen.getByRole("dialog");
}

describe("the API access card", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 10, 2, 12, 0));
  });
  afterEach(() => vi.useRealTimers());

  it("grants access with no end date and reloads", async () => {
    show(NO_ACCESS);
    mocked.adminGrantApiAccess.mockResolvedValue(GRANTED.entitlements[0]);
    const dialog = await openGrantDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "Grant access" }));
    await waitFor(() => expect(mocked.adminGrantApiAccess).toHaveBeenCalledWith("u1", null));
    await waitFor(() => expect(mocked.adminUserApiAccess).toHaveBeenCalledTimes(2));
  });

  it("grants access to the end of a picked day, from today on", async () => {
    show(NO_ACCESS);
    mocked.adminGrantApiAccess.mockResolvedValue(GRANTED.entitlements[0]);
    const dialog = await openGrantDialog();
    const input = within(dialog).getByLabelText("Last day of access (optional)");
    expect(input.getAttribute("min")).toBe("2026-11-02");
    fireEvent.change(input, { target: { value: "2026-12-01" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Grant access" }));
    await waitFor(() => expect(mocked.adminGrantApiAccess).toHaveBeenCalledWith("u1", new Date(2026, 11, 2).toISOString()));
  });

  it("revokes the grant after the confirm", async () => {
    show(GRANTED);
    mocked.adminRevokeApiGrant.mockResolvedValue({ ...GRANTED.entitlements[0], lapsed_at: "2026-11-02T10:00:00Z" });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke grant" }));
    expect(screen.getByText(/keys are kept and refused until it has access again/)).toBeTruthy();
    expect(mocked.adminRevokeApiGrant).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Revoke grant" }));
    await waitFor(() => expect(mocked.adminRevokeApiGrant).toHaveBeenCalledWith("e1"));
  });

  it("revokes a key after the confirm", async () => {
    show(GRANTED, [KEY]);
    mocked.adminRevokeApiKey.mockResolvedValue({ ...KEY, revoked_at: "2026-11-02T10:00:00Z" });
    expect(await screen.findByText("rtk_ab12cd34_…")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Revoke Nightly export" }));
    expect(mocked.adminRevokeApiKey).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(mocked.adminRevokeApiKey).toHaveBeenCalledWith("k1"));
  });
});
