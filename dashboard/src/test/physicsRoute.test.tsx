import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Sidebar from "../components/Sidebar";

// The Physics item is in the signed-in nav, so the mocked caller has a session.
const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" } as { name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));
const flags = vi.hoisted(() => ({ realOnly: false }));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
// A getter, so each render reads the case's surface rather than the one the
// module saw at load.
vi.mock("../pages/map/utils/domains", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pages/map/utils/domains")>()),
  get usesRealOnlyFeed() {
    return flags.realOnly;
  },
}));

function renderSidebar(realOnly: boolean) {
  flags.realOnly = realOnly;
  return render(
    <MemoryRouter>
      <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

describe("the Physics Layer route", () => {
  it("is offered on a synthetic surface", () => {
    const { container } = renderSidebar(false);
    expect(container.querySelector('a[href="/physics"]')).toHaveTextContent("Physics Layer");
  });

  it("is absent on a real-radar surface", () => {
    const { container } = renderSidebar(true);
    expect(container.querySelector('a[href="/physics"]')).toBeNull();
  });
});
