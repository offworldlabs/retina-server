import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ErrorBoundary from "../components/ErrorBoundary";

const crash = { on: true };

function Boom() {
  if (crash.on) throw new Error("boom");
  return <p>recovered</p>;
}

// React logs every error a boundary catches, and jsdom reports it again as an
// uncaught error event unless the event is cancelled.
const cancel = (e: ErrorEvent) => e.preventDefault();
let quiet: MockInstance;
beforeEach(() => {
  crash.on = true;
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
    crash.on = false;
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
