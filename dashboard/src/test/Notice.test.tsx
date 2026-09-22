import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FetchNotice, Notice, nothingLoaded } from "../components/Notice";

const at = new Date(2026, 8, 22, 14, 5, 9);

describe("Notice", () => {
  it("is a warning in the stylesheet's markup unless told otherwise", () => {
    render(<Notice>Files before 2026-09-01 are in cold storage.</Notice>);
    const notice = screen.getByRole("alert");
    expect(notice.className).toBe("notice");
    expect(notice).toHaveTextContent("cold storage");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers a retry when given one", () => {
    const retry = vi.fn();
    render(<Notice tone="error" onRetry={retry}>Could not save.</Notice>);
    expect(screen.getByRole("alert")).toHaveClass("notice", "error");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});

describe("FetchNotice", () => {
  const refresh = vi.fn();

  it("says nothing while the fetch succeeds", () => {
    const { container } = render(
      <FetchNotice polled={{ error: null, updatedAt: at, refresh }} what="alerts" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("stands in for the page when nothing has loaded", () => {
    const polled = { error: new Error("HTTP 502"), updatedAt: null, refresh };
    expect(nothingLoaded(polled)).toBe(true);
    render(<FetchNotice polled={polled} what="alerts" />);
    const notice = screen.getByRole("alert");
    expect(notice).toHaveClass("notice", "error");
    expect(notice).toHaveTextContent("Could not load alerts: HTTP 502");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refresh).toHaveBeenCalled();
  });

  it("says how old the answer on screen is when a refresh fails", () => {
    const polled = { error: new Error("HTTP 502"), updatedAt: at, refresh };
    expect(nothingLoaded(polled)).toBe(false);
    render(<FetchNotice polled={polled} what="alerts" />);
    const notice = screen.getByRole("alert");
    expect(notice.className).toBe("notice");
    expect(notice).toHaveTextContent("Could not refresh alerts: HTTP 502");
    expect(notice).toHaveTextContent(`loaded at ${at.toLocaleTimeString()}`);
  });

  it("does not count a fetch still in flight as nothing loaded", () => {
    expect(nothingLoaded({ error: null, updatedAt: null, refresh })).toBe(false);
  });
});
