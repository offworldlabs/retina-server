import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../App";
import ErrorBoundary from "../components/ErrorBoundary";
import { ThemeProvider } from "../context/ThemeContext";

const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" },
    loading: false,
    syntheticFleet: false,
    logout: async () => ({ redirected: false }),
  },
  crash: true,
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
vi.mock("../pages/user/OverviewPage", () => ({
  default: () => {
    if (state.crash) throw new Error("overview boom");
    return <div>overview page</div>;
  },
}));
vi.mock("../pages/user/LeaderboardPage", () => ({ default: () => <div>leaderboard page</div> }));

function Boom() {
  if (state.crash) throw new Error("boom");
  return <p>recovered</p>;
}

// React logs every error a boundary catches, and jsdom reports it again as an
// uncaught error event unless the event is cancelled.
const cancel = (e: ErrorEvent) => e.preventDefault();
let quiet: MockInstance;
beforeEach(() => {
  state.crash = true;
  quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  window.addEventListener("error", cancel);
});
afterEach(() => {
  quiet.mockRestore();
  window.removeEventListener("error", cancel);
});

describe("the error fallback", () => {
  it("offers a retry and a reload in the shared button styles", () => {
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong");
    expect(screen.getByRole("button", { name: "Try again" })).toHaveClass("btn", "btn-primary");
    expect(screen.getByRole("button", { name: "Reload page" })).toHaveClass("btn", "btn-outline");
  });

  it("draws the children again on a retry", () => {
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    state.crash = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("recovered")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reloads the document", () => {
    const realLocation = window.location;
    const reload = vi.fn();
    Object.defineProperty(window, "location", { value: { reload }, writable: true, configurable: true });
    try {
      render(<ErrorBoundary><Boom /></ErrorBoundary>);
      fireEvent.click(screen.getByRole("button", { name: "Reload page" }));
      expect(reload).toHaveBeenCalledOnce();
    } finally {
      Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
    }
  });
});

/** The whole console at `at`. */
function visit(at: string) {
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[at]}>
        <App />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("a page that throws while rendering", () => {
  it("keeps the sidebar, which still navigates to a page that draws", async () => {
    const { container } = visit("/overview");
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on this page");
    expect(container.querySelector(".header-title")).toHaveTextContent("Overview");
    fireEvent.click(screen.getByRole("link", { name: /^leaderboard$/i }));
    expect(await screen.findByText("leaderboard page")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("draws once more on a retry", async () => {
    visit("/overview");
    await screen.findByRole("alert");
    state.crash = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("overview page")).toBeInTheDocument();
  });
});
