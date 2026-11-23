import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import LoginBack from "../components/LoginBack";
import appCss from "../App.css?raw";
import { stubViewport } from "./matchMedia";

const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");

function renderBack() {
  render(
    <MemoryRouter>
      <LoginBack />
    </MemoryRouter>
  );
  return screen.getByRole("button", { name: /Back to the map/ });
}

describe("the login card's Back button", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is a 44px target raised so its label stays where it was on a phone", () => {
    stubViewport(390);
    const back = renderBack();
    expect(back.style.top).toBe("4px");
    expect(back.style.minHeight).toBe("44px");
  });

  it("is the same on a tablet driven by touch", () => {
    stubViewport(800, { coarse: true });
    const back = renderBack();
    expect(back.style.top).toBe("4px");
    expect(back.style.minHeight).toBe("44px");
  });

  it.each([
    ["a tablet with a fine pointer", 800],
    ["a desktop", 1280],
  ])("has no style of its own on %s", (_name, width) => {
    stubViewport(width);
    expect(renderBack().hasAttribute("style")).toBe(false);
  });

  it("ends inside the card's 48px top padding", () => {
    const tops = [...strip(appCss).matchAll(/(?:^|[ }])\.login-card \{[^}]*?padding: (\S+)/g)].map((m) => m[1]);
    expect(tops).toEqual(["48px", "48px"]);
  });
});
