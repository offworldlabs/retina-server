import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SCALAR_THEME_CSS } from "../utils/scalarTheme";

let theme: "light" | "dark" = "light";

// The page keeps the bundle's load in module state, so each test takes a fresh copy.
async function freshPage() {
  vi.resetModules();
  vi.doMock("../context/ThemeContext", () => ({ useResolvedTheme: () => theme }));
  return import("../pages/admin/ApiDocsPage");
}

function injected(): HTMLScriptElement | null {
  return document.head.querySelector<HTMLScriptElement>('script[src*="scalar-api-reference"]');
}

// What the bundle does as it runs: define window.Scalar, then the script's load fires.
async function bundleLoads(createApiReference: (el: HTMLElement, config: object) => ScalarInstance) {
  window.Scalar = { createApiReference };
  await act(async () => {
    injected()!.dispatchEvent(new Event("load"));
  });
}

async function bundleFails() {
  await act(async () => {
    injected()!.dispatchEvent(new Event("error"));
  });
}

function scalar() {
  const destroy = vi.fn();
  const createApiReference = vi.fn((el: HTMLElement, _config: object) => {
    // What a mounted instance leaves behind, which the next must not hydrate.
    el.appendChild(document.createElement("div"));
    return { destroy };
  });
  return { createApiReference, destroy };
}

describe("ApiDocsPage", () => {
  beforeEach(() => {
    delete window.Scalar;
    theme = "light";
  });

  afterEach(() => {
    injected()?.remove();
  });

  it("loads the vendored bundle from its versioned public path", async () => {
    const { default: ApiDocsPage } = await freshPage();
    render(<ApiDocsPage />);
    expect(injected()!.getAttribute("src")).toBe("/vendor/scalar-api-reference-1.69.0/standalone.js");
  });

  it("mounts Scalar on the whole schema, uploads off, in RETINA's palette, and destroys it on leaving", async () => {
    const { createApiReference, destroy } = scalar();
    const { default: ApiDocsPage, DOCUMENT_URL } = await freshPage();

    const view = render(<ApiDocsPage />);
    await bundleLoads(createApiReference);

    expect(DOCUMENT_URL).toBe("/api/admin/openapi.json");
    expect(createApiReference).toHaveBeenCalledTimes(1);
    expect(createApiReference.mock.calls[0][1]).toMatchObject({
      url: DOCUMENT_URL,
      agent: { disabled: true },
      mcp: { disabled: true },
      withDefaultFonts: false,
      telemetry: false,
      theme: "none",
      customCss: SCALAR_THEME_CSS,
      forceDarkModeState: "light",
    });

    view.unmount();
    expect(destroy).toHaveBeenCalledTimes(1);
  });

  it("re-creates Scalar in the new theme, into an emptied element", async () => {
    const { createApiReference, destroy } = scalar();
    const { default: ApiDocsPage } = await freshPage();

    const view = render(<ApiDocsPage />);
    await bundleLoads(createApiReference);
    theme = "dark";
    await act(async () => {
      view.rerender(<ApiDocsPage />);
    });

    expect(destroy).toHaveBeenCalledTimes(1);
    expect(createApiReference).toHaveBeenCalledTimes(2);
    const [el, config] = createApiReference.mock.calls[1];
    expect(config).toMatchObject({ forceDarkModeState: "dark" });
    expect(el.children).toHaveLength(1);
  });

  it("says so when the bundle does not load", async () => {
    const { default: ApiDocsPage } = await freshPage();

    render(<ApiDocsPage />);
    await bundleFails();

    expect(screen.getByText(/did not load/)).toBeInTheDocument();
    expect(injected()).toBeNull();
  });

  it("mounts and clears the error when a retry after a failed load succeeds", async () => {
    const { createApiReference } = scalar();
    const { default: ApiDocsPage } = await freshPage();

    const view = render(<ApiDocsPage />);
    await bundleFails();
    expect(screen.getByText(/did not load/)).toBeInTheDocument();

    theme = "dark";
    await act(async () => {
      view.rerender(<ApiDocsPage />);
    });
    await bundleLoads(createApiReference);

    expect(createApiReference).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/did not load/)).not.toBeInTheDocument();
  });
});
