import { describe, it, expect } from "vitest";

import { signInNext } from "../utils/signInNext";

// The cases backend/tests/test_magic_link_routes.py holds the server's rule
// to. Nothing checks one list against the other, so change them together.
describe("the page to open once signed in", () => {
  it.each(["/settings", "/nodes/ret-0042", "/sim/physics", "/data/2026/09/17"])("accepts %s", (path) => {
    expect(signInNext(path)).toBe(path);
  });

  it.each([
    "//evil.example.com",
    "//evil.example.com/settings",
    "/\\evil.example.com",
    "https://evil.example.com/",
    "javascript:alert(1)",
    "settings",
    "/settings?tab=security",
    "/settings#danger",
    "/set tings",
    "/%2F%2Fevil.example.com",
    "/" + "a".repeat(300),
    "",
  ])("refuses %s", (path) => {
    expect(signInNext(path)).toBeNull();
  });

  it.each([null, undefined, 42, { next: "/settings" }])("refuses a non-string (%s)", (value) => {
    expect(signInNext(value)).toBeNull();
  });
});
