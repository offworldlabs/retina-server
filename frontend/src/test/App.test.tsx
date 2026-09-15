import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";

describe("App", () => {
  it("renders the header", () => {
    render(<App />);
    expect(screen.getByText("RETINA")).toBeInTheDocument();
  });

  it("opens on the live radar tab", () => {
    render(<App />);
    expect(document.querySelector(".app-body.live-active")).toBeTruthy();
  });
});
