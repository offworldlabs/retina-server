import { describe, it, expect, beforeEach, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DashboardLayout from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";
import { stubMatchMedia } from "./matchMedia";

const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" } as { name: string; email: string } | null,
    loading: false,
    // The simulator is routed, and so named, only where the server runs a
    // fleet, and the /sim cases below are about how it is named.
    syntheticFleet: true,
    logout: async () => ({ redirected: false }),
  },
}));

// Sidebar and Header both call useAuth, which throws outside AuthProvider; the
// house pattern (signedOutChrome.test.tsx) mocks it rather than mounting one.
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

function renderAt(path: string, isAdmin = false) {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <DashboardLayout isAdmin={isAdmin}><div>page</div></DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("the map's content pane", () => {
  beforeEach(() => stubMatchMedia());

  it("is flush on the map route", () => {
    const { container } = renderAt("/map");
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  it("is flush on the simulation map", () => {
    const { container } = renderAt("/sim", true);
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  // Nested under /sim, and the rule is read off the first segment, so this is
  // the case that would quietly regress if that rule were narrowed to /sim
  // exactly.
  it("is flush on the physics page under it", () => {
    const { container } = renderAt("/sim/physics", true);
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  it("keeps its padding everywhere else", () => {
    const { container } = renderAt("/detections");
    expect(container.querySelector(".content.flush")).toBeNull();
    expect(container.querySelector(".content")).not.toBeNull();
  });
});

describe("the header's name for a page", () => {
  beforeEach(() => stubMatchMedia());

  // The sidebar carries most of these words too, so read the header alone.
  const titleAt = (path: string, isAdmin: boolean) =>
    renderAt(path, isAdmin).container.querySelector(".header-title")?.textContent;

  // Every other page is named by its first path segment and owns whatever
  // nests under it. /sim is the exception: the page beneath it configures the
  // simulator rather than being a view of it, so the table is consulted with
  // the whole path first.
  it("names the simulation map", () => {
    expect(titleAt("/sim", true)).toBe("Simulation Map");
  });

  it("names the physics page nested under it, not its parent", () => {
    expect(titleAt("/sim/physics", true)).toBe("Physics Layer");
  });

  it("names a node's own page, not the list above it", () => {
    expect(titleAt("/nodes/nde0example0001", false)).toBe("Node Detail");
  });

  it("names a node's own page on the admin surface too, where /nodes is the list", () => {
    expect(titleAt("/nodes", true)).toBe("Node Management");
    expect(titleAt("/nodes/nde0example0001", true)).toBe("Node Detail");
  });

  it.each([
    ["/mlat", "MLAT Verification"],
    ["/infrastructure", "Infrastructure"],
    ["/api-docs", "API Reference"],
  ])("names the admin page at %s", (path, title) => {
    expect(titleAt(path, true)).toBe(title);
  });

  it("names a page that owns its subtree by its first segment", () => {
    expect(titleAt("/sim/anything", true)).toBe("Simulation Map");
  });
});
