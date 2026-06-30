import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";

test.describe("Portfolio security", () => {
  test("portfolio API is auth-gated and cockpit shows locked state", async ({ page, request }) => {
    const { consoleErrors, pageErrors, failedApiResponses } = attachStrictClientMonitors(page);

    const listed = await request.get("/api/portfolio");
    expect(listed.status()).toBe(401);

    const created = await request.post("/api/portfolio", {
      data: {
        symbol: `ZZE2E${Date.now()}`,
        asset_type: "stock",
        quantity: 0.0001,
        buy_price: 1,
        buy_date: "2026-01-15",
      },
    });
    expect(created.status()).toBe(401);

    const del = await request.delete("/api/portfolio/1");
    expect(del.status()).toBe(401);

    await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#ppositions")).toContainText("Sign in", { timeout: 20_000 });

    expect(consoleErrors, `console: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, JSON.stringify(failedApiResponses)).toHaveLength(0);
  });
});
