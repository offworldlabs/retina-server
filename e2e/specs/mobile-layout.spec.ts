/**
 * The console on phones and tablets: every page at 390, 360, 768 and 820 px,
 * each in Chromium and in WebKit.
 *
 * Each page test opens one page against the running stack, staged as the
 * desktop screenshots stage theirs (fixtures/routes.ts: the light theme, the
 * OpenStreetMap basemap on fixed tiles, sockets answered in the page, the
 * clock moved only until the page settles, or until the settle budget runs
 * out on a page that has loaded everything but never stops changing, which
 * the test notes), and measures it once
 * (fixtures/layoutProbe.ts) against the width its project emulates. It fails,
 * naming the element and the numbers, on:
 *
 *   - a page wider than the screen, or a console pane that scrolls sideways;
 *   - an element cut off at the right edge outside a scroller of its own;
 *   - a control that takes taps across less than 44×44 px;
 *   - on a phone or a touch tablet, a field set under 16 px, which iOS zooms into on focus;
 *   - on a tablet, a table column hinted hidden that still shows;
 *
 * and before any of them, on a page that sent its owner or administrator to
 * the sign-in page, which would be measured in its place, and on a project
 * that does not report a coarse pointer, since the touch rules would go
 * untested there. The exemptions each rule allows are listed, with their
 * reasons, in fixtures/layoutProbe.ts, and a test notes in its annotations
 * each one it applied.
 *
 * The shell's own tests, once per console: up to 768 px, Menu opens the
 * drawer and the scrim and Escape close it; on a phone, the appearance switch
 * is in the drawer and not in the header; from 769 px, the sidebar is the
 * 64 px rail and there is no Menu.
 *
 * The eight projects exist only under E2E_ENV=local, against the dev server
 * on E2E_LOCAL_PORT. The backend serves either the admin console's pages or
 * the owner's and the signed-out ones (fixtures/liveStack.ts), so the suite
 * runs once in each phase, and each run skips the other phase's tests, saying
 * which phase it found. A page whose path needs a value E2E_STATE lacks skips.
 *
 * From e2e/, with the stack up in either phase:
 *
 *   E2E_ENV=local E2E_LOCAL_PORT=… E2E_STATE=… E2E_OWNER_STATE=… E2E_MAIL_LOG=… \
 *     npx playwright test specs/mobile-layout.spec.ts
 *
 * E2E_CHROMIUM_PATH names the Chromium build to launch where Playwright's own
 * is missing; without it the Chromium projects drive the installed Chrome.
 */
import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";
import { EDGE_EXEMPTIONS, TOUCH_EXEMPTIONS, probeLayout, type EdgeExemption, type LayoutReport } from "../fixtures/layoutProbe";
import { PHASE_OF, requireKnownPhase, test } from "../fixtures/liveStack";
import {
  FIRST_SETTLE_MS,
  PAGE_CONTEXT,
  ROUTES,
  loadSeedState,
  missingValues,
  resolvePath,
  settlePage,
  siteOrigin,
  stagePage,
  type PageWatch,
  type Role,
  type Site,
} from "../fixtures/routes";

test.use({
  locale: PAGE_CONTEXT.locale,
  timezoneId: PAGE_CONTEXT.timezoneId,
  colorScheme: PAGE_CONTEXT.colorScheme,
  serviceWorkers: PAGE_CONTEXT.serviceWorkers,
  contextOptions: { reducedMotion: PAGE_CONTEXT.reducedMotion },
});

// Loading the page and settling it may take a minute each in WebKit.
const TEST_TIMEOUT_MS = 150_000;

// The console's breakpoints (dashboard/src/styles/breakpoints.ts): a phone's
// layout to 640 px, the sidebar a drawer to 768, a tablet's to 1023.
const PHONE_MAX = 640;
const DRAWER_MAX = 768;
const TABLET_MAX = 1023;

/** How far the held clock moves while a shell test waits on the page. */
const TICK_MS = 100;

/** A visual viewport scale below this is a page zoomed out, not rounding. */
const MIN_SCALE = 0.99;

const STATE = loadSeedState();

/** The screen the test's project emulates, from its configuration. */
function screenOf(testInfo: TestInfo): { width: number; height: number } {
  const viewport = testInfo.project.use.viewport;
  if (!viewport) throw new Error(`${testInfo.project.name} sets no viewport, so there is no screen width to measure against`);
  return viewport;
}

const PHASE_SKIP = {
  app:
    "app phase only: this backend admits every caller as the administrator (the admin phase), " +
    "so no owner or signed-out page can be opened",
  admin:
    "admin phase only: this backend refuses a caller without a session (the app phase), " +
    "so the admin console cannot be opened as its anonymous administrator",
} as const;

/** Declares, in the enclosing group, that its tests open pages as `role`:
 *  skipped before any page opens when the backend runs the other phase, and
 *  signed in as the owner for the owner's. */
function openedAs(role: Role): void {
  const wanted = PHASE_OF[role];
  test.skip(({ phase }) => phase.kind !== "unknown" && phase.kind !== wanted, PHASE_SKIP[wanted]);
  if (role === "owner") {
    test.use({
      storageState: async ({ ownerState }, use) => {
        await use(ownerState ?? undefined);
      },
    });
  }
}

/** Fails, before anything is measured or tapped, when the page is not the one
 *  its role was meant to see: the settled page is the sign-in page, or the
 *  backend, asked from inside the page so the browser's own cookies are used,
 *  does not see the session the role needs. Several owner pages draw a
 *  signed-out view rather than redirect, so only that answer tells them apart. */
async function requireRoleSession(page: Page, where: string, role: Role): Promise<void> {
  if (role === "owner" || role === "admin") {
    const pathname = await page.evaluate(() => location.pathname);
    const who = role === "owner" ? "owner" : "administrator";
    const lost = role === "owner" ? "the session did not reach the browser" : "the backend did not admit the browser as its anonymous administrator";
    expect(pathname, `${where}: the page sent the ${who} to the sign-in page, so ${lost}`).not.toBe("/login");
  }
  const me = await page.evaluate(async () => {
    const r = await fetch("/api/auth/me", { credentials: "same-origin" });
    let body: { role?: string; auth_enabled?: boolean } | null = null;
    try {
      body = await r.json();
    } catch {
      // not JSON: the status alone speaks
    }
    return { status: r.status, role: body?.role ?? null, authEnabled: body?.auth_enabled ?? null };
  });
  if (role === "owner") {
    const ok = me.status === 200 && me.authEnabled !== false && me.role !== "admin";
    expect(
      ok,
      `${where}: the browser has no owner session (GET /api/auth/me answered ${me.status}${me.role ? `, role ${me.role}` : ""}), so the page would be measured signed out`,
    ).toBe(true);
  } else if (role === "admin") {
    expect(
      me.status === 200 && me.authEnabled === false,
      `${where}: the backend did not admit the browser as its anonymous administrator (GET /api/auth/me answered ${me.status})`,
    ).toBe(true);
  } else {
    expect(
      me.status,
      `${where}: the browser has a session (GET /api/auth/me answered ${me.status}), so the signed-out page cannot be measured`,
    ).toBe(401);
  }
}

/** Lists in the test's annotations what the page did that the run withheld or
 *  that went wrong in it; none of it fails the test by itself. */
function listTrouble(testInfo: TestInfo, watch: PageWatch): void {
  const lists: [string, string[]][] = [
    ["aborted off-origin request", watch.offsite],
    ["failed API request", watch.failedApi],
    ["page error", watch.errors],
  ];
  for (const [type, list] of lists) for (const description of list) testInfo.annotations.push({ type, description });
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** Every rule's verdict on `report`, each a soft assertion naming the offenders. */
function checkLayout(where: string, testInfo: TestInfo, width: number, report: LayoutReport): void {
  const { scrollWidth, innerWidth, visual, content } = report.page;
  const overflow: string[] = [];
  if (scrollWidth - width > 1) {
    overflow.push(`the page is ${scrollWidth} px wide, ${scrollWidth - width} px wider than the ${width} px screen`);
  }
  if (innerWidth !== width) {
    overflow.push(`the browser laid the page out ${innerWidth} px wide instead of ${width} px, to fit what overflows`);
  }
  // Zoom is read from the scale, not the visible width: WebKit on macOS
  // draws a scrollbar beside a document that scrolls, which narrows the
  // visible area by its width at a scale of 1.
  if (visual !== null && visual.scale < MIN_SCALE) {
    overflow.push(
      `the page is zoomed out to a scale of ${visual.scale.toFixed(2)}, showing ${Math.round(visual.width)} px ` +
        `of a ${innerWidth} px wide layout on the ${width} px screen, to fit what overflows`,
    );
  }
  if (content && content.scrollWidth - content.clientWidth > 1) {
    overflow.push(
      `the console pane (.content) scrolls sideways by ${content.scrollWidth - content.clientWidth} px ` +
        `(scroll width ${content.scrollWidth}, client width ${content.clientWidth})`,
    );
  }
  expect.soft(overflow.length, `${where}: sideways overflow\n  ${overflow.join("\n  ")}`).toBe(0);

  const cut = report.cutOff.map(
    (c) =>
      `${c.path}${c.text ? ` "${c.text}"` : ""}: ${c.past} px past ` +
      `${c.edge === null ? `the screen's edge at ${c.edgeAt} px` : `${c.edge}, which ends at ${c.edgeAt} px`}; ` +
      `box ${c.width}×${c.height}`,
  );
  expect.soft(cut.length, `${where}: cut off at the right edge, outside a scroller of their own\n  ${cut.join("\n  ")}`).toBe(0);

  const { probed, misses, exempt, notDrawn, pannedOut, underPanel, overlapped } = report.touch;
  const small = misses.map(
    (m) =>
      `${m.path}${m.text ? ` "${m.text}"` : ""}: box ${m.box.width}×${m.box.height}, ` +
      `takes taps across ${m.hit.width}×${m.hit.height}; the probe ${m.probe} landed on ${m.landedOn}` +
      (m.outside ? " (the control is off the screen even scrolled to)" : ""),
  );
  expect.soft(small.length, `${where}: touch targets under 44×44 px, ${small.length} of ${probed}\n  ${small.join("\n  ")}`).toBe(0);
  testInfo.annotations.push({
    type: "touch targets",
    description:
      `${probed} probed, ${misses.length} under 44×44 px; not reported: ${plural(overlapped, "map marker")} ` +
      `overlapped by other map data; not probed: ${notDrawn} inside a visually hidden box, ` +
      `${pannedOut} panned out of view in a drag-panned strip, ${underPanel} under a panel over a drag-panned strip`,
  });
  for (const exemption of TOUCH_EXEMPTIONS) {
    const covered = exempt[exemption.name] ?? 0;
    if (covered > 0) {
      testInfo.annotations.push({
        type: "touch-target exemption",
        description: `${exemption.name}: ${plural(covered, "control")} (${exemption.reason})`,
      });
    }
  }
  for (const [exemption, reason] of Object.entries(EDGE_EXEMPTIONS) as [EdgeExemption, string][]) {
    const covered = report.edgeExempt[exemption] ?? 0;
    if (covered > 0) {
      testInfo.annotations.push({ type: "right-edge exemption", description: `${exemption}: ${plural(covered, "element")} (${reason})` });
    }
  }

  if (width <= TABLET_MAX) {
    const fields = report.smallFields.map((f) => `${f.path} (${f.type}): ${f.px} px`);
    expect.soft(fields.length, `${where}: fields under 16 px on a phone or touch tablet, which iOS zooms into\n  ${fields.join("\n  ")}`).toBe(0);
  }
  if (width > PHONE_MAX && width <= TABLET_MAX) {
    const columns = report.hiddenColumnsShown.map((c) => `${c.path}${c.text ? ` "${c.text}"` : ""}: display ${c.display}`);
    expect.soft(columns.length, `${where}: table columns hinted hidden that still show\n  ${columns.join("\n  ")}`).toBe(0);
  }
}

// ── Every page ──────────────────────────────────────────────────────────────

for (const route of ROUTES) {
  const path = resolvePath(route, STATE);
  const missing = missingValues(route, STATE);
  const reason = { type: "missing seeded value", description: `the page's path needs ${missing.join(", ")} from E2E_STATE` };
  test.describe(() => {
    openedAs(route.role);
    (path === null ? test.skip : test)(
      `${route.id} fits a touch screen`,
      { annotation: path === null ? [reason] : [] },
      async ({ page, phase }, testInfo) => {
        test.setTimeout(TEST_TIMEOUT_MS);
        requireKnownPhase(phase);
        // Never true here, given the skip above; it narrows the type.
        if (path === null) return;
        const { width } = screenOf(testInfo);
        const where = `${route.id} at ${testInfo.project.name} (${width} px)`;

        const watch = await stagePage(page, { t0: Date.now() });
        try {
          await page.goto(`${siteOrigin(route.site)}${path}`, { waitUntil: "load", timeout: FIRST_SETTLE_MS });
          // A page that has loaded everything but keeps changing is measured
          // as it stands: its layout is the one a finger meets.
          await settlePage(page, watch, FIRST_SETTLE_MS, {
            onStillChanging: () =>
              testInfo.annotations.push({
                type: "still changing",
                description:
                  `nothing was loading or in flight, but the page did not stay unchanged long enough to count as settled ` +
                  `within ${FIRST_SETTLE_MS / 1000} s, so it was measured as it stood`,
              }),
          });
          await requireRoleSession(page, where, route.role);
          const report = await page.evaluate(probeLayout, {
            width,
            fields: width <= TABLET_MAX,
            tablet: width > PHONE_MAX && width <= TABLET_MAX,
            exemptions: TOUCH_EXEMPTIONS,
          });
          expect(
            report.coarsePointer,
            `${testInfo.project.name} does not emulate touch: (pointer: coarse) does not match, ` +
              "so the touch-only styles and the touch rules would go untested",
          ).toBe(true);
          checkLayout(where, testInfo, width, report);
        } finally {
          listTrouble(testInfo, watch);
        }
      },
    );
  });
}

// ── The shell ───────────────────────────────────────────────────────────────

/** A page of each console that has the sidebar, opened as its role. */
const SHELLS: readonly { site: Site; role: Role; path: string }[] = [
  { site: "app", role: "owner", path: "/overview" },
  { site: "admin", role: "admin", path: "/" },
];

const MENU = 'button.header-menu[aria-label="Menu"]';
const SIDEBAR = "#console-sidebar";

/** Moves the held clock a step, for anything the shell waits on a timer for. */
async function tick(page: Page, watch: PageWatch): Promise<void> {
  try {
    await page.clock.runFor(TICK_MS);
  } catch (error) {
    // The first error a timer threw; the clock still reached the step's end.
    watch.errors.push(String(error instanceof Error ? error.message : error).split("\n")[0]);
  }
}

interface Box {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
}

async function boxOf(locator: Locator): Promise<Box> {
  return locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width };
  });
}

/** Where `locator` lies against the screen: wholly on it, wholly off it, or
 *  partly, with its edges. */
async function placement(page: Page, watch: PageWatch, locator: Locator, screen: { width: number; height: number }): Promise<string> {
  await tick(page, watch);
  const b = await boxOf(locator);
  const edges = `left ${Math.round(b.left)}, right ${Math.round(b.right)}, top ${Math.round(b.top)}, bottom ${Math.round(b.bottom)}`;
  if (b.left >= -0.5 && b.top >= -0.5 && b.right <= screen.width + 0.5 && b.bottom <= screen.height + 0.5) return "on the screen";
  if (b.right <= 0.5 || b.left >= screen.width - 0.5) return "off the screen";
  return `partly on the ${screen.width}×${screen.height} screen (${edges})`;
}

/** Where the focus is: in the drawer, or on what. */
async function focus(page: Page, watch: PageWatch): Promise<string> {
  await tick(page, watch);
  return page.evaluate((sidebar) => {
    const active = document.activeElement;
    if (active?.closest(sidebar)) return "in the drawer";
    return active ? `on <${active.tagName.toLowerCase()} class="${active.getAttribute("class") ?? ""}">` : "nowhere";
  }, SIDEBAR);
}

/** Opens the drawer with Menu and checks it opened. Menu is looked for
 *  first, so a page without one fails in seconds, not at the test's timeout. */
async function openDrawer(page: Page): Promise<void> {
  const menu = page.locator(MENU);
  await expect(menu, "the header shows Menu").toBeVisible();
  await menu.click();
  await expect(menu, "Menu says the drawer is open").toHaveAttribute("aria-expanded", "true");
}

for (const shell of SHELLS) {
  test.describe(() => {
    openedAs(shell.role);

    /** Opens the shell's page, staged and settled, and runs `body` on it. */
    const onShell = async (page: Page, testInfo: TestInfo, body: (watch: PageWatch) => Promise<void>) => {
      const watch = await stagePage(page, { t0: Date.now() });
      try {
        await page.goto(`${siteOrigin(shell.site)}${shell.path}`, { waitUntil: "load", timeout: FIRST_SETTLE_MS });
        await settlePage(page, watch, FIRST_SETTLE_MS);
        await requireRoleSession(page, `${shell.site} shell (${shell.path}) at ${testInfo.project.name}`, shell.role);
        await body(watch);
      } finally {
        listTrouble(testInfo, watch);
      }
    };

    test.describe(() => {
      test.skip(({ viewport }) => (viewport?.width ?? 0) > DRAWER_MAX, `the sidebar is in the row above ${DRAWER_MAX} px`);
      test(`${shell.site} shell: Menu opens the drawer, and the scrim and Escape close it`, async ({ page, phase }, testInfo) => {
        test.setTimeout(TEST_TIMEOUT_MS);
        requireKnownPhase(phase);
        const screen = screenOf(testInfo);
        await onShell(page, testInfo, async (watch) => {
          const menu = page.locator(MENU);
          const drawer = page.locator(SIDEBAR);
          const scrim = page.locator(".sidebar-scrim");
          await expect(menu, "the header shows Menu").toBeVisible();
          await expect(menu, "Menu says the drawer is closed").toHaveAttribute("aria-expanded", "false");

          await openDrawer(page);
          await expect.poll(() => placement(page, watch, drawer, screen), { message: "the open drawer lies wholly on the screen" }).toBe("on the screen");
          await expect(scrim, "the scrim covers the page beside the drawer").toBeVisible();
          await expect.poll(() => focus(page, watch), { message: "focus moves into the drawer" }).toBe("in the drawer");

          // A tap on the scrim halfway between the drawer's edge and the screen's.
          const open = await boxOf(drawer);
          const shade = await boxOf(scrim);
          const x = (Math.max(open.right, shade.left) + Math.min(shade.right, screen.width)) / 2;
          const y = (Math.max(shade.top, 0) + Math.min(shade.bottom, screen.height)) / 2;
          expect(x - open.right, `the scrim shows beside the drawer, which ends at ${Math.round(open.right)} px`).toBeGreaterThan(4);
          await scrim.click({ position: { x: x - shade.left, y: y - shade.top } });
          await expect(menu, "a tap on the scrim closes the drawer").toHaveAttribute("aria-expanded", "false");
          await expect(scrim, "the scrim goes with the drawer").toBeHidden();
          await expect.poll(() => placement(page, watch, drawer, screen), { message: "the closed drawer leaves the screen" }).toBe("off the screen");

          await openDrawer(page);
          await page.keyboard.press("Escape");
          await expect(menu, "Escape closes the drawer").toHaveAttribute("aria-expanded", "false");
          await expect(menu, "Escape hands the focus back to Menu").toBeFocused();
          await expect.poll(() => placement(page, watch, drawer, screen), { message: "the closed drawer leaves the screen" }).toBe("off the screen");
        });
      });
    });

    test.describe(() => {
      test.skip(({ viewport }) => (viewport?.width ?? 0) > PHONE_MAX, `the header keeps the appearance switch above ${PHONE_MAX} px`);
      test(`${shell.site} shell: the appearance switch is in the drawer, not the header`, async ({ page, phase }, testInfo) => {
        test.setTimeout(TEST_TIMEOUT_MS);
        requireKnownPhase(phase);
        await onShell(page, testInfo, async () => {
          await expect(page.locator(".header .theme-switch"), "a phone's header leaves the appearance switch to the drawer").toBeHidden();
          await openDrawer(page);
          await expect(page.locator(`${SIDEBAR} .theme-switch`), "the open drawer holds the appearance switch").toBeVisible();
        });
      });
    });

    test.describe(() => {
      test.skip(({ viewport }) => {
        const width = viewport?.width ?? 0;
        return width <= DRAWER_MAX || width > TABLET_MAX;
      }, `the sidebar is the rail from ${DRAWER_MAX + 1} to ${TABLET_MAX} px`);
      test(`${shell.site} shell: the sidebar is a 64 px rail, and there is no Menu`, async ({ page, phase }, testInfo) => {
        test.setTimeout(TEST_TIMEOUT_MS);
        requireKnownPhase(phase);
        const screen = screenOf(testInfo);
        await onShell(page, testInfo, async (watch) => {
          const rail = page.locator(SIDEBAR);
          await expect(rail, "the sidebar is shown").toBeVisible();
          await expect
            .poll(
              async () => {
                await tick(page, watch);
                const { width } = await boxOf(rail);
                return Math.abs(width - 64) <= 1 ? "64 px wide" : `${Math.round(width * 10) / 10} px wide`;
              },
              { message: "with nothing stored, a tablet's sidebar is the 64 px rail" },
            )
            .toBe("64 px wide");
          await expect.poll(() => placement(page, watch, rail, screen), { message: "the rail lies wholly on the screen" }).toBe("on the screen");
          await expect(page.locator("button.header-menu"), "the header has no Menu beside the rail").toBeHidden();
        });
      });
    });
  });
}
