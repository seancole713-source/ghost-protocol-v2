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
