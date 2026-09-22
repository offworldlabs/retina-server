import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ui from "@retina/shared/css/ui.css?raw";

vi.mock("../api/client", () => ({
  api: { adminEvents: vi.fn(), alerts: vi.fn(), adminUsers: vi.fn(), adminNodeOwners: vi.fn() },
}));

import { api } from "../api/client";
import EventsPage from "../pages/admin/EventsPage";
import UserManagementPage from "../pages/admin/UserManagementPage";
import AlertsPage from "../pages/user/AlertsPage";

/* A badge's tone says how things stand: green is healthy, amber wants a look,
   red is in trouble. Severity and role are not health, so neither borrows a
   health tone it does not mean. */

const events = [
  { ts: 1, severity: "info", category: "node", message: "node connected" },
  { ts: 2, severity: "warning", category: "node", message: "node slow" },
  { ts: 3, severity: "critical", category: "node", message: "node lost" },
];

const badge = (text: string) => screen.getByText(text, { selector: ".badge" });

describe("badge tones", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("defines an info tone in the shared stylesheet", () => {
    expect(ui).toMatch(/\.badge\.info\s*\{/);
  });

  it.each([
    ["Events", EventsPage, "adminEvents"],
    ["Alerts", AlertsPage, "alerts"],
  ] as const)("shows %s info severity in the info tone, not the healthy one", async (_name, Page, call) => {
    vi.mocked(api[call]).mockResolvedValue(events);
    render(<Page />);
    await screen.findByText("node connected");
    expect(badge("info")).toHaveClass("badge", "info");
    expect(badge("info")).not.toHaveClass("online");
    expect(badge("warning")).toHaveClass("warning");
    expect(badge("critical")).toHaveClass("offline");
  });

  it("shows a role as a tag, with admin in no warning tone", async () => {
    vi.mocked(api.adminUsers).mockResolvedValue([
      { id: "1", name: "Ada", email: "ada@example.com", provider: "email", role: "admin" },
      { id: "2", name: "Bob", email: "bob@example.com", provider: "email", role: "user" },
    ]);
    vi.mocked(api.adminNodeOwners).mockResolvedValue({});
    render(<UserManagementPage />);
    await screen.findByText("Ada");
    for (const role of ["admin", "user"]) {
      expect(badge(role)).toHaveClass("badge", "plain");
      expect(badge(role)).not.toHaveClass("warning");
      expect(badge(role)).not.toHaveClass("online");
    }
  });
});
