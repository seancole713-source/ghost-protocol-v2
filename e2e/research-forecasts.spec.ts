import { expect, test } from "@playwright/test";

test("recorded research forecasts stay separate from approved picks", async ({ page }) => {
  await page.goto("/console");
  const pending = page.waitForResponse(response =>
    response.url().includes("/api/forecasts/research?limit=100"));
  await page.locator('nav button[data-section="research"]').click();
  const response = await pending;
  expect(response.status()).toBe(200);
  const report = await response.json();
  expect(report.trading_eligible).toBe(false);
  expect(report.accuracy_proven).toBe(false);
  const section = page.locator("#section-research");
  await expect(section.getByRole("heading", { name: "Research forecasts" })).toBeVisible();
  await expect(section).toContainText("not proven 70% accurate");
  if (report.forecasts.length) {
    await expect(section.getByText(`Research #${report.forecasts[0].id}`, { exact: true })).toBeVisible();
    await expect(section).toContainText("Not trading eligible");
  } else {
    await expect(section).toContainText("No open recorded research forecasts");
  }
});


test("consumer page links to the actual research view", async ({ page }) => {
  await page.goto("/picks");
  await page.getByRole("link", { name: "Research forecasts (not approved picks)" }).click();
  await expect(page).toHaveURL(/\/console#research$/);
  await expect(page.locator("#section-research")).toBeVisible();
});

test("clear safety latch does not imply an approved model", async ({ page }) => {
  await page.route("**/api/v3/status", route => route.fulfill({json:{fleet_summary:{fireable_now:0}}}));
  await page.route("**/api/wolf/kill-status", route => route.fulfill({json:{ok:true,engine_pause:{paused:false}}}));
  await page.goto("/picks");
  await expect(page.locator("#view-today")).toContainText("No approved models");
  await page.getByRole("tab", {name:"System",exact:true}).click();
  await expect(page.locator("#view-system")).toContainText("No approved models");
});

test("Today: degraded coverage is not a clean No, and the three lanes stay separate", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const nowSec = Math.floor(Date.now() / 1000);
  await page.route("**/api/squeeze/picks", route => route.fulfill({json:{
    ok: true, enabled: true, radar_active: true, scan_ok: true, snapshot_stale: false,
    last_scan_ts: nowSec - 30, symbols: 105, fetch_ok: 6,
    scanned_symbols: 105, usable_symbols: 6, coverage_degraded: true, picks: [],
  }}));
  await page.route("**/api/picks?limit=5", route => route.fulfill({json:{
    ok: true, core_engine_mode: "research", active: [],
  }}));
  await page.route("**/api/forecasts/research?limit=5", route => route.fulfill({json:{
    ok: true, trading_eligible: false, accuracy_proven: false, total_open: 1,
    forecasts: [{ id: 7, symbol: "<b>XSS</b>", direction: "UP", entry_reference: 10,
      target_reference: 11, stop_reference: 9.5, trading_eligible: false }],
  }}));
  await page.goto("/picks");
  const today = page.locator("#view-today");
  await expect(today).toContainText("Insufficient coverage (6 of 105 symbols usable)");
  await expect(today.locator(".verdict .a")).not.toHaveText(/^No\.?$/);
  await expect(today.getByRole("heading", { name: "Approved core picks" })).toBeVisible();
  await expect(today.getByRole("heading", { name: "Experimental / paper forecasts" })).toBeVisible();
  await expect(today.getByRole("heading", { name: "Unvalidated radar observations" })).toBeVisible();
  await expect(today).toContainText("<b>XSS</b>"); // rendered as text, not markup
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
