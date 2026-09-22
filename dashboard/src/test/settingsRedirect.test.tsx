import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import App from "../App";
import { ThemeProvider } from "../context/ThemeContext";

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({
    user: { name: "Ada", email: "ada@example.com" },
    loading: false,
    syntheticFleet: false,
    logout: async () => ({ redirected: false }),
  }),
}));

// The route is what is under test, not the page's fetches.
vi.mock("../pages/user/OnboardingPage", () => ({ default: () => <div>my nodes page</div> }));

function Where() {
  const { pathname } = useLocation();
  return <output aria-label="location">{pathname}</output>;
}

describe("the old /settings address", () => {
  it("lands on My Nodes", async () => {
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as typeof window.matchMedia;
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={["/settings"]}>
          <App />
          <Where />
        </MemoryRouter>
      </ThemeProvider>,
    );
    expect(await screen.findByText("my nodes page")).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("/onboarding");
  });
});
