import { expect, test } from "@playwright/test";
import { blockPortfolioMutations } from "./read-only-portfolio";

test("live portfolio guard blocks mutations before any network fallback", async ({ page }) => {
  const forwarded: string[] = [];
  // Every possible fallback is mocked; this regression never contacts an app.
  await page.route("**/*", async route => {
    forwarded.push(route.request().method());
    await route.fulfill({ status: 200, contentType: "application/json",
      headers: { "access-control-allow-origin": "*" }, body: "{}" });
  });
  const blocked = await blockPortfolioMutations(page);
  await page.goto("https://portfolio.invalid/");
  forwarded.length = 0;  // navigation was mocked; later requests are same-origin
  await page.evaluate(async () => {
    for (const method of ["POST", "DELETE"]) {
      await fetch("https://portfolio.invalid/api/portfolio/1", { method }).catch(() => null);
    }
  });
  expect(blocked).toEqual(["POST", "DELETE"]);
  expect(forwarded).toEqual([]);
  await page.evaluate(() => fetch("https://portfolio.invalid/api/portfolio").then(r => r.json()));
  expect(forwarded).toEqual(["GET"]);
});
