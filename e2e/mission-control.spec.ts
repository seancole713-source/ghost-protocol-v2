/**
 * Real-browser verification of the Mission Control forensic-audit fixes
 * (MC-1..4). Loads the actual branch admin.html in Chromium, stubs the 8
 * dashboard endpoints with realistic production-shaped JSON, and asserts the
 * tiles render honestly and safely. Point at a static server for the branch:
 *   BASE_URL=http://127.0.0.1:8099 npx playwright test e2e/mission-control.spec.ts --project=chromium
 */
import { test, expect } from "@playwright/test";

// These specs load raw .html files from a STATIC server (branch verification),
// so they only make sense against a local host — production serves admin at
// /admin (cookie-gated), not /admin.html. Skip when pointed at a remote host.
const BASE = process.env.BASE_URL || "";
const IS_LOCAL = /127\.0\.0\.1|localhost/.test(BASE);
test.skip(!IS_LOCAL, "branch static-file verification — set BASE_URL to a local static server");

const MC_STUBS: Record<string, any> = {
  "/admin/health": { ok: true, score: 95, status: "healthy", issues: [], warnings: [] },
  "/api/research/status": { ok: true, research_active: false, resolved_picks: 63 },
  "/api/system/breakers": {
    ok: true,
    // One OPEN breaker whose name carries an XSS payload — MC-3 must escape it.
    breakers: {
      yfinance: { state: "closed" },
      alpaca: { state: "closed" },
      "<img src=x onerror=window.__xss=1>": { state: "open" },
    },
  },
  "/api/wolf/kill-status": {
    ok: true,
    any_triggered: true,
    resolved_available: 30,
    engine_pause: { paused: true, reason: "win_rate->auto_pause" },
  },
  "/api/v3/status": {
    ok: true,
    trained: true,
    models: 100,
    // The post-research-tier reality: many serveable, none fireable.
    fleet_summary: { serveable: 191, serveable_research: 180, fireable_now: 0 },
    symbols: {},
  },
  "/api/squeeze/picks": { ok: true, radar_active: false, fetch_ok: 0, symbols: 100, pick_count: 0 },
  "/api/system/degraded": { ok: true, degraded: false, reasons: [] },
  "/api/wolf/super-ghost/accuracy": {
    ok: true,
    total_logged: 1540,
    resolved_at_horizon: 500,
    overall: { n: 500, wins: 191, win_rate: 0.382 },
  },
};

test.describe("Mission Control audit fixes", () => {
  test("MC-1/3: v3 tile headlines fireable (not total models); breaker name escaped", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
    page.on("pageerror", (e) => consoleErrors.push(String(e)));

    // Stub every dashboard call; specific MC endpoints get realistic shapes,
    // everything else a benign {ok:true} so the page can't error on load.
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === new URL(page.url() || "http://127.0.0.1:8099").origin && /\.(js|css|html|ico)$/.test(url.pathname)) {
        return route.continue();
      }
      const match = Object.keys(MC_STUBS).find((k) => url.pathname === k || url.pathname.startsWith(k));
      if (match) return route.fulfill({ json: MC_STUBS[match] });
      if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/admin/")) {
        return route.fulfill({ json: { ok: true } });
      }
      return route.continue();
    });

    await page.goto("/admin.html", { waitUntil: "domcontentloaded" });
    // loadMissionControl() runs on load; wait for the board to populate.
    await page.waitForFunction(() => {
      const b = document.getElementById("mc-board");
      return b && b.querySelectorAll(".mc-tile").length >= 8;
    }, { timeout: 15000 });

    const board = page.locator("#mc-board");
    const boardHtml = await board.innerHTML();

    // The v3 tile: find the tile whose label is "v3 engine".
    const v3Tile = page.locator(".mc-tile", { has: page.locator(".mc-label", { hasText: "v3 engine" }) });
    const v3Value = await v3Tile.locator(".mc-value").innerText();
    const v3Sub = await v3Tile.locator(".mc-sub").innerText();

    // MC-1: headline is fireable count, not "100 models".
    expect(v3Value).toContain("0 fireable");
    expect(v3Value).not.toContain("100 model");
    // MC-1: fireable_now=0 must NOT be green.
    await expect(v3Tile).not.toHaveClass(/mc-ok/);
    await expect(v3Tile).toHaveClass(/mc-warn/);
    // MC-1: the research split is surfaced honestly.
    expect(v3Sub).toContain("191 serveable");
    expect(v3Sub).toContain("180 research");

    // MC-3: the XSS breaker name is rendered escaped, not executed.
    expect(await page.evaluate(() => (window as any).__xss)).toBeUndefined();
    expect(boardHtml).toContain("&lt;img");
    // and NOT double-escaped (escHtml is centralized in mcTile now).
    expect(boardHtml).not.toContain("&amp;lt;img");
    // no real img element injected into the board
    expect(await board.locator("img").count()).toBe(0);

    // Truth-ledger tile stays honest: shows the real 38% WR (not hidden/inflated).
    expect(boardHtml).toMatch(/WR 38%/);

    expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
  });

  // Contract-70 evidence UI commits (cockpit/console/picks) — verify the
  // branch pages parse and render without JS errors in a real browser.
  for (const pageFile of ["cockpit.html", "ghost_console.html", "picks.html"]) {
    test(`${pageFile} loads without JS/console errors`, async ({ page }) => {
      const errs: string[] = [];
      page.on("console", (m) => m.type() === "error" && errs.push(m.text()));
      page.on("pageerror", (e) => errs.push(String(e)));
      await page.route("**/*", async (route) => {
        const u = new URL(route.request().url());
        if (/\.(js|css|html|ico|png|svg)$/.test(u.pathname)) return route.continue();
        if (u.pathname.startsWith("/api/") || u.pathname.startsWith("/admin/")) {
          return route.fulfill({ json: { ok: true } });
        }
        return route.continue();
      });
      await page.goto(`/${pageFile}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(1500); // let async loaders run against stubs
      // Ignore benign network-abort noise from stubbed streams; fail on real JS errors.
      // Exclude environment artifacts of the static-only harness: the real
      // server provides /ws/cockpit + streaming endpoints this file server does
      // not, so WS-handshake and network aborts here are not branch defects.
      const realErrs = errs.filter((e) => !/Failed to load resource|net::ERR|aborted|WebSocket/i.test(e));
      expect(realErrs, `JS errors in ${pageFile}: ${realErrs.join(" | ")}`).toEqual([]);
    });
  }
});
