import { describe, expect, it } from "vitest";
import appSource from "../App.tsx?raw";
import routesSource from "../routes.tsx?raw";
import tableSource from "../../../e2e/fixtures/routes.ts?raw";

/* CI runs these tests but not the Playwright suite, so a page added to the
   router and left out of the E2E route table is checked at phone width by
   nobody, and nothing says so. This reads the router's paths and the table's
   rows as text and asks that every console page, and every sign-in page, has
   a row for its site. */

type Row = { site: string; path: string };

const block = (source: string, from: string, to: string): string => {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start);
  if (start < 0 || end < 0) throw new Error(`no "${from}" ... "${to}" block in the source`);
  return source.slice(start, end);
};

const pathsIn = (text: string, pattern: RegExp): string[] => Array.from(text.matchAll(pattern), (m) => m[1]);

const userPaths = pathsIn(
  block(routesSource, "const userRoutes", "const adminRoutes"),
  /\{\s*path:\s*"([^"]+)"/g,
).filter((path) => path !== "/test-radar");

const adminPaths = pathsIn(
  block(routesSource, "const adminRoutes", "export function consoleRoutes"),
  /\{\s*path:\s*"([^"]+)"/g,
);

const authPaths = pathsIn(appSource, /<Route\s+path="([^"]+)"/g).filter((path) => path !== "/*");

const rows: Row[] = Array.from(
  tableSource.matchAll(/\{\s*id:\s*"[^"]+",\s*site:\s*"(\w+)",\s*path:\s*(?:"([^"]*)"|`([^`]*)`)/g),
  (m) => ({ site: m[1], path: m[2] ?? m[3] }),
);

const segments = (path: string): string[] => path.split(/[?#]/)[0].split("/");

// A `:param` segment takes any one non-empty segment; every other must be equal.
const matches = (pattern: string, path: string): boolean => {
  const want = segments(pattern);
  const got = segments(path);
  return want.length === got.length && want.every((part, i) => (part.startsWith(":") ? got[i] !== "" : part === got[i]));
};

const missing = (site: string, paths: string[]): string[] =>
  paths.filter((path) => !rows.some((row) => row.site === site && matches(path, row.path))).map((path) => `${site} ${path}`);

const expectCovered = (gaps: string[]) =>
  expect(gaps, `pages with no row in e2e/fixtures/routes.ts, add one for each:\n${gaps.join("\n")}`).toEqual([]);

describe("E2E route table coverage", () => {
  it("reads every page and row from the sources", () => {
    expect(userPaths.length).toBeGreaterThanOrEqual(17);
    expect(adminPaths.length).toBeGreaterThanOrEqual(18);
    expect(authPaths.length).toBeGreaterThanOrEqual(4);
    expect(rows.length).toBeGreaterThanOrEqual(43);
  });

  it("matches a parameter segment to one non-empty segment only", () => {
    expect(matches("/nodes/:nodeId", "/nodes/{P1_REF}")).toBe(true);
    expect(matches("/map", "/map#t=2026-10-18T20:00:00Z&span=5")).toBe(true);
    expect(matches("/nodes/:nodeId", "/nodes")).toBe(false);
    expect(matches("/nodes/:nodeId", "/nodes/")).toBe(false);
    expect(matches("/nodes/:nodeId", "/nodes/a/b")).toBe(false);
    expect(matches("/", "/map")).toBe(false);
  });

  it("every console page has a row in the e2e route table", () => {
    expectCovered(missing("app", userPaths));
  });

  it("every admin console page has a row in the e2e route table", () => {
    expectCovered(missing("admin", adminPaths));
  });

  it("every sign-in page has a row in the e2e route table", () => {
    expectCovered(missing("app", authPaths));
  });
});
