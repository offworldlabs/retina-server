import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useScopeParam } from "../hooks/useScopeParam";

/** Reads the hook and the location it writes, and drives its setter. */
function Probe({ available }: { available: boolean }) {
  const [scope, setScope] = useScopeParam(available);
  const { search } = useLocation();
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  return (
    <div>
      <span data-testid="scope">{scope}</span>
      <span data-testid="search">{search}</span>
      <span data-testid="navigation">{navigationType}</span>
      <button type="button" onClick={() => setScope("network")}>Network</button>
      <button type="button" onClick={() => setScope("mine")}>Mine</button>
      <button type="button" onClick={() => navigate(-1)}>Back</button>
    </div>
  );
}

function renderAt(entry: string, available = true) {
  render(
    <MemoryRouter initialEntries={["/overview", entry]} initialIndex={1}>
      <Routes>
        <Route path="/overview" element={<div>Overview</div>} />
        <Route path="/detections" element={<Probe available={available} />} />
      </Routes>
    </MemoryRouter>,
  );
}

const scope = () => screen.getByTestId("scope").textContent;
const search = () => screen.getByTestId("search").textContent;

describe("useScopeParam", () => {
  it("reads the owner's nodes by default", () => {
    renderAt("/detections");
    expect(scope()).toBe("mine");
  });

  it("reads the whole network from ?scope=network", () => {
    renderAt("/detections?hours=6&scope=network");
    expect(scope()).toBe("network");
  });

  it.each(["?scope=mine", "?scope=", "?scope=NETWORK", "?scope=everything", "?hours=6"])(
    "reads anything else, such as %s, as the owner's nodes",
    (query) => {
      renderAt(`/detections${query}`);
      expect(scope()).toBe("mine");
    },
  );

  it.each(["", "?scope=mine", "?scope=network"])("reads the whole network when there is no owner view, at '%s'", (query) => {
    renderAt(`/detections${query}`, false);
    expect(scope()).toBe("network");
  });

  it("writes ?scope=network and keeps every other parameter, in order", () => {
    renderAt("/detections?node=nde1&node=nde2&hours=6");
    fireEvent.click(screen.getByRole("button", { name: "Network" }));

    expect(scope()).toBe("network");
    expect(search()).toBe("?node=nde1&node=nde2&hours=6&scope=network");
  });

  it("writes the default by removing the parameter, and keeps the others", () => {
    renderAt("/detections?hours=6&scope=network&node=nde1");
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));

    expect(scope()).toBe("mine");
    expect(search()).toBe("?hours=6&node=nde1");
  });

  it("leaves the bare path when the scope was the only parameter", () => {
    renderAt("/detections?scope=network");
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));

    expect(search()).toBe("");
  });

  it("replaces the history entry, so Back leaves the page rather than flipping the switch", () => {
    renderAt("/detections?hours=6");
    fireEvent.click(screen.getByRole("button", { name: "Network" }));
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    fireEvent.click(screen.getByRole("button", { name: "Network" }));
    expect(screen.getByTestId("navigation").textContent).toBe("REPLACE");

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Overview")).toBeInTheDocument();
  });
});
