import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import KnowledgeBasePage from "../pages/user/KnowledgeBasePage";
import LeaderboardPage from "../pages/user/LeaderboardPage";
import { DISCORD_URL, WEBSITE_URL } from "../utils/community";
import { resetAuth } from "./fakeAuth";

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

beforeEach(() => {
  resetAuth();
  vi.unstubAllGlobals();
});

function serveEmptyLeaderboard() {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ leaderboard: [], total: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
}

describe("the community links", () => {
  it("point the Knowledge Base at the shared Discord invite and website", () => {
    render(
      <MemoryRouter>
        <KnowledgeBasePage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Community Discord" }).getAttribute("href")).toBe(DISCORD_URL);
    expect(screen.getByRole("link", { name: "Website" }).getAttribute("href")).toBe(WEBSITE_URL);
  });

  it("point the Leaderboard at the same Discord invite and website", async () => {
    serveEmptyLeaderboard();
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByRole("link", { name: "Join Discord" })).toBeTruthy());
    expect(screen.getByRole("link", { name: "Join Discord" }).getAttribute("href")).toBe(DISCORD_URL);
    expect(screen.getByRole("link", { name: "Website" }).getAttribute("href")).toBe(WEBSITE_URL);
  });
});

/* Read as text, tests excluded. A page that spells a link out again would pass
   the render checks above while drifting from the module. The apex domain is
   matched as a name with no label in front of it, so a subdomain written into
   prose (adsb.retina.fm in the map's settings) is not a second spelling. */
const sources = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).filter(([path]) => !path.startsWith("../test/") && !/\.test\.tsx?$/.test(path)),
);

function filesMatching(pattern: RegExp) {
  return Object.entries(sources)
    .filter(([, source]) => pattern.test(source))
    .map(([path]) => path);
}

describe("the source sweep", () => {
  it("finds sources to check, so a glob that matched nothing cannot pass", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
    expect(Object.keys(sources)).toContain("../utils/community.ts");
  });

  it("keeps the Discord invite in community.ts alone", () => {
    expect(filesMatching(/discord\.gg/)).toEqual(["../utils/community.ts"]);
  });

  it("spells the apex domain in community.ts alone", () => {
    expect(filesMatching(/(?<![\w-]\.)(?<![\w-])retina\.fm/)).toEqual(["../utils/community.ts"]);
  });
});
