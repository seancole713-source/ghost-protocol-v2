import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";

test.describe("Portfolio persistence", () => {
  test("POST position appears in GET and cockpit UI; DELETE removes it", async ({ page, request }) => {
    const sym = `ZZE2E${Date.now()}`;
    const { consoleErrors, pageErrors, failedApiResponses } = attachStrictClientMonitors(page);

    const created = await request.post("/api/portfolio", {
      data: {
        symbol: sym,
        asset_type: "crypto",
        quantity: 0.0001,
        buy_price: 1,
        buy_date: "2026-01-15",
      },
    });
    expect(created.ok()).toBeTruthy();
    const cj = await created.json();
    expect(cj.ok).toBeTruthy();
    const id = cj.id as number;
    expect(typeof id).toBe("number");

    const listed = await request.get("/api/portfolio");
    expect(listed.ok()).toBeTruthy();
    const lj = await listed.json();
    const found = (lj.positions ?? []).some((p: { symbol: string }) => p.symbol === sym);
    expect(found, "new symbol in GET /api/portfolio").toBeTruthy();

    await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    await page.locator("#tab-portfolio").click();
    await expect(page.locator("#ppositions")).toContainText(sym, { timeout: 20_000 });

    const del = await request.delete(`/api/portfolio/${id}`);
    expect(del.ok()).toBeTruthy();

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("#tab-portfolio").click();
    await expect(page.locator("#ppositions")).not.toContainText(sym, { timeout: 15_000 });

    expect(consoleErrors, `console: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, JSON.stringify(failedApiResponses)).toHaveLength(0);
  });
});
