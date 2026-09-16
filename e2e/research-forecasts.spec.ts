import { expect, test } from "@playwright/test";

test("recorded research forecasts stay separate from approved picks", async ({ page }) => {
  await page.goto("/picks");
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
