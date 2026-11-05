import { expect, test } from "./safe-test";
import { blockLiveMutations } from "./read-only-browser";

test("live guard blocks canonical and encoded writes before network fallback", async ({ page, context }) => {
  const forwarded: string[] = [];
  // Every fallback is mocked. No app, provider or real portfolio is contacted.
  await context.route("**/*", async route => {
    forwarded.push(route.request().method());
    await route.fulfill({ status: 200,
      contentType: route.request().isNavigationRequest() ? "text/html" : "application/json",
      body: route.request().isNavigationRequest() ? '<html><head><link rel="icon" href="data:,"></head></html>' : "{}" });
  });
  const blocked = await blockLiveMutations(context);
  await page.goto("https://portfolio.invalid/");
  forwarded.length = 0;
  await page.evaluate(async () => {
    for (const path of ["/api/portfolio/1", "/api/%70ortfolio/1"]) {
      for (const method of ["POST", "DELETE", "PUT", "PATCH"]) {
        await fetch(path, { method }).catch(() => null);
      }
    }
  });
  expect(blocked).toEqual(["POST", "DELETE", "PUT", "PATCH", "POST", "DELETE", "PUT", "PATCH"]);
  expect(forwarded).toEqual([]);
  await page.evaluate(() => fetch("/api/portfolio").then(r => r.json()));
  expect(forwarded).toEqual(["GET"]);
  expect(context.serviceWorkers()).toEqual([]);
});

test("live API fixture rejects mutations without contacting an app", async ({ request }) => {
  const url = "https://portfolio.invalid/api/portfolio/1";
  await expect(request.post(url)).rejects.toThrow("Live API mutations are forbidden");
  await expect(request.delete(url)).rejects.toThrow("Live API mutations are forbidden");
  await expect(request.put(url)).rejects.toThrow("Live API mutations are forbidden");
  await expect(request.patch(url)).rejects.toThrow("Live API mutations are forbidden");
  await expect(request.fetch(url, { method: "POST" })).rejects.toThrow("Live API mutations are forbidden");
});
