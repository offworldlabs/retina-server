import { describe, it, expect, vi, afterEach } from "vitest";
import { waitFor } from "@testing-library/react";
import indexHtml from "../../index.html?raw";

/* The browser bar's colour follows the theme on screen: the OS scheme under
   `system`, the chosen theme otherwise. The entry point does this as it runs,
   so each test runs it afresh on a page carrying index.html's own tags. */

// Mounting the app is not what these are about, only the entry point's own
// top-level work.
vi.mock("react-dom/client", () => ({ default: { createRoot: () => ({ render: () => {} }) } }));

const LIGHT = "#ffffff";
const DARK = "#132240";
const root = document.documentElement;

/** Each tag's colour, the light scheme's first. */
const barColours = () =>
  [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')].map((m) => m.content);

/** Runs the entry point with `stamped` as theme-boot.js left the attribute. */
async function start(stamped: "light" | "dark" | null) {
  vi.resetModules();
  document.head.innerHTML = indexHtml.match(/<meta name="theme-color"[^>]*>/g).join("");
  if (stamped) root.setAttribute("data-theme", stamped);
  await import("../main");
}

afterEach(() => {
  root.removeAttribute("data-theme");
  document.head.innerHTML = "";
});

describe("the browser bar's colour", () => {
  it("takes a theme chosen before the app started", async () => {
    await start("dark");
    expect(barColours()).toEqual([DARK, DARK]);
  });

  it("follows a choice made in the app, and goes back to the OS for system", async () => {
    await start(null);
    expect(barColours()).toEqual([LIGHT, DARK]);

    root.setAttribute("data-theme", "dark");
    await waitFor(() => expect(barColours()).toEqual([DARK, DARK]));

    root.setAttribute("data-theme", "light");
    await waitFor(() => expect(barColours()).toEqual([LIGHT, LIGHT]));

    root.removeAttribute("data-theme");
    await waitFor(() => expect(barColours()).toEqual([LIGHT, DARK]));
  });
});
