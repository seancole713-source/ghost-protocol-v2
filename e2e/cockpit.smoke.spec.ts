import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";

test.describe("Cockpit smoke", () => {
  test("cockpit page renders core elements and title", async ({ page }) => {
    const { consoleErrors, pageErrors, failedApiResponses } = attachStrictClientMonitors(page);

    const response = await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    expect(response?.ok()).toBeTruthy();

    // PR #77: updated selectors to match redesigned cockpit (June 2026).
    // Old IDs (#cgrid, #stxt, #tab-stocks, etc.) no longer exist.
    await expect(page.locator("#movers-board")).toBeVisible({ timeout: 25_000 });
    await expect(page.locator("#ghost-score-wrap")).toBeVisible();
    await expect(page.locator("#deploy-badge")).toBeVisible();
    await expect(page.locator("#f-open")).toBeVisible();

    await expect(page.locator(".logo")).toContainText("GHOST PROTOCOL");

    expect(consoleErrors, `Console errors found: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, JSON.stringify(failedApiResponses)).toHaveLength(0);
  });

  test("core API endpoints return valid payloads", async ({ request }) => {
    const stats = await request.get("/api/stats");
    expect(stats.ok()).toBeTruthy();
    const statsBody = await stats.json();
    expect(statsBody.ok).toBeTruthy();
    expect(typeof statsBody.wins).toBe("number");
    expect(typeof statsBody.losses).toBe("number");
    // scan_symbols: enforced in unit tests; production E2E uses default BASE_URL which may lag one deploy.

    const cockpit = await request.get("/api/cockpit/context");
    expect(cockpit.ok()).toBeTruthy();
    const cockpitBody = await cockpit.json();
    expect(cockpitBody.ok).toBeTruthy();
    expect(cockpitBody.stats.wins).toBe(statsBody.wins);
    expect(cockpitBody.stats.losses).toBe(statsBody.losses);
    if (statsBody.scan_symbols && cockpitBody.stats.scan_symbols) {
      expect(cockpitBody.stats.scan_symbols).toEqual(statsBody.scan_symbols);
      expect(Array.isArray(statsBody.scan_symbols.stocks)).toBeTruthy();
    }

    const health = await request.get("/health");
    expect(health.ok()).toBeTruthy();
  });
});
