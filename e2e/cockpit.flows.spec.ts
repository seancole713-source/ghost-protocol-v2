import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";

/** Evidence-oriented UI checks against live BASE_URL (desktop + mobile projects). */
test.describe("Cockpit flows", () => {
  test("tabs switch panels; truth toggle; portfolio validation; reload survives", async ({ page }) => {
    const { consoleErrors, pageErrors, failedApiResponses } = attachStrictClientMonitors(page);

    await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#movers-board")).toBeVisible({ timeout: 25_000 });
    await expect(page.locator("#mvr-tiers")).not.toContainText("Loading squeeze radar", { timeout: 25_000 });

    await page.locator("#mvr-toggle").click();
    await expect(page.locator("#ghost-score-wrap")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("#squeeze-picks-section")).toBeVisible();
    await expect(page.locator("#portfolio-section")).toBeVisible();

    const body = page.locator("#truth-body");
    const toggle = page.locator("#truth-toggle");
    await expect(body).not.toHaveClass(/collapsed/);
    await toggle.click();
    await expect(body).toHaveClass(/collapsed/);
    await toggle.click();
    await expect(body).not.toHaveClass(/collapsed/);

    await page.locator("#add-pos-toggle").click();
    await page.locator("#p-sym").fill("");
    await page.locator("#p-qty").fill("");
    await page.locator("#p-bp").fill("");
    await page.locator("#port-form .btn-add").click();
    await expect(page.locator("#perr")).toContainText("Fill in symbol");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator(".logo")).toContainText("GHOST PROTOCOL");
    await expect(page.locator("#movers-board")).toBeVisible();

    expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, JSON.stringify(failedApiResponses)).toHaveLength(0);
  });

  test("POST /api/portfolio requires auth before validation", async ({ request }) => {
    const r = await request.post("/api/portfolio", {
      data: {
        symbol: "",
        asset_type: "stock",
        quantity: 1,
        buy_price: 10,
        buy_date: "2026-01-01",
      },
    });
    expect(r.status()).toBe(401);
    const j = await r.json();
    expect(j.detail || j.error).toBeTruthy();
  });
});
