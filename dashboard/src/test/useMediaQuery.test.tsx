import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { PHONE } from "../styles/breakpoints";
import { stubViewport } from "./matchMedia";

function Probe() {
  return <p>{useMediaQuery(PHONE) ? "phone" : "wide"}</p>;
}

describe("useMediaQuery", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("answers for the screen on the first render", () => {
    stubViewport(390);
    render(<Probe />);
    expect(screen.getByText("phone")).toBeInTheDocument();
  });

  it("redraws when the screen crosses the width", () => {
    const viewport = stubViewport(641);
    render(<Probe />);
    expect(screen.getByText("wide")).toBeInTheDocument();
    viewport.resize(640);
    expect(screen.getByText("phone")).toBeInTheDocument();
    viewport.resize(1024);
    expect(screen.getByText("wide")).toBeInTheDocument();
  });

  it("reads false where there is no matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    render(<Probe />);
    expect(screen.getByText("wide")).toBeInTheDocument();
  });

  it("stops listening when it unmounts", () => {
    const viewport = stubViewport(390);
    const { unmount } = render(<Probe />);
    expect(viewport.listenerCount()).toBe(1);
    unmount();
    expect(viewport.listenerCount()).toBe(0);
  });
});
