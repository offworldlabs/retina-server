import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api/client", () => ({
  api: { myApiAccess: vi.fn(), myApiKeys: vi.fn(), createApiKey: vi.fn(), revokeApiKey: vi.fn() },
}));

import { api } from "../../api/client";
import type { ApiAccessStatus, ApiKeyInfo } from "../../types";
import ApiAccessPage from "./ApiAccessPage";

const LIMITS = { tier: "contributor", requests_per_minute: 60, max_concurrent_streams: 2, max_history_span_s: 604800 };
const CONTRIBUTOR = { active_days: 20, window_days: 30, threshold_days: 15, grace_ends_at: null };
const ACCESS: ApiAccessStatus = {
  entitled: true,
  kind: "contributor",
  reason: null,
  limits: LIMITS,
  contributor: CONTRIBUTOR,
  grant_ends_at: null,
};
const KEY: ApiKeyInfo = {
  id: "k1",
  name: "Nightly export",
  prefix: "ab12cd34",
  created_at: "2026-10-30T10:00:00Z",
  last_used_at: null,
  revoked_at: null,
};
const SECRET = "rtk_ab12cd34_exampleexampleexampleexampleexampleexa";

const mocked = vi.mocked(api);

function show(access: ApiAccessStatus, keys: ApiKeyInfo[] = []) {
  mocked.myApiAccess.mockResolvedValue(access);
  mocked.myApiKeys.mockResolvedValue(keys);
  render(
    <MemoryRouter>
      <ApiAccessPage />
    </MemoryRouter>,
  );
}

describe("the API access page", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows an entitled contributor's days and offers Create key", async () => {
    show(ACCESS);
    expect(await screen.findByText(/reported data on 20 of the last 30 days/)).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText(/60 requests a minute, 2 open streams, history spans up to 7 days/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create key" })).toBeTruthy();
  });

  it("tells an account without access how to qualify, from the response's numbers", async () => {
    show({ ...ACCESS, entitled: false, kind: null, reason: "lapsed", limits: null, contributor: { ...CONTRIBUTOR, active_days: 9 } });
    expect(await screen.findByText(/at least 15 of the last 30 days/)).toBeTruthy();
    expect(screen.getByText(/Your best node: 9 of 30 days/)).toBeTruthy();
    expect(screen.getByText("Not active")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create key" })).toBeNull();
  });

  it("gives an administrator full access, no keys and no Create key", async () => {
    show({ entitled: true, kind: "admin", reason: null, limits: { ...LIMITS, tier: "admin" }, contributor: null, grant_ends_at: null });
    expect(await screen.findByText("Full access")).toBeTruthy();
    expect(screen.getByText(/Keys belong to accounts/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create key" })).toBeNull();
    expect(mocked.myApiKeys).not.toHaveBeenCalled();
  });

  it("shows a created key once, then only its prefix", async () => {
    show(ACCESS);
    mocked.createApiKey.mockResolvedValue({ ...KEY, key: SECRET });
    fireEvent.click(await screen.findByRole("button", { name: "Create key" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Nightly export" } });
    mocked.myApiKeys.mockResolvedValue([KEY]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Create key" }));

    expect(await screen.findByDisplayValue(SECRET)).toBeTruthy();
    expect(mocked.createApiKey).toHaveBeenCalledWith("Nightly export");
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
    expect(screen.getByText(/only time the key is shown/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByDisplayValue(SECRET)).toBeNull();
    expect(document.body.textContent).not.toContain(SECRET);
    expect(await screen.findByText("rtk_ab12cd34_…")).toBeTruthy();
  });

  it("revokes a key after asking, and the row shows Revoked", async () => {
    show(ACCESS, [KEY]);
    mocked.revokeApiKey.mockResolvedValue({ ...KEY, revoked_at: "2026-11-01T09:00:00Z" });
    fireEvent.click(await screen.findByRole("button", { name: "Revoke Nightly export" }));
    expect(screen.getByText(/Anything using it stops working at once/)).toBeTruthy();
    mocked.myApiKeys.mockResolvedValue([{ ...KEY, revoked_at: "2026-11-01T09:00:00Z" }]);
    fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));

    await waitFor(() => expect(mocked.revokeApiKey).toHaveBeenCalledWith("k1"));
    expect(await screen.findByText(/^Revoked/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Revoke Nightly export" })).toBeNull();
  });
});
