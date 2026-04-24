import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";

/** Evidence-oriented UI checks against live BASE_URL (desktop + mobile projects). */
test.describe("Cockpit flows", () => {
  test("tabs switch panels; truth toggle; portfolio validation; reload survives", async ({ page }) => {
    const { consoleErrors, pageErrors, failedApiResponses } = attachStrictClientMonitors(page);

    await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#cgrid")).not.toContainText("Loading plays", { timeout: 25_000 });
    expect(await page.locator("#panel-crypto").evaluate((el) => el.classList.contains("active"))).toBeTruthy();

    const tabs: Array<"stocks" | "portfolio" | "results" | "news" | "crypto"> = [
      "stocks",
      "portfolio",
      "results",
      "news",
      "crypto",
    ];
    for (const id of tabs) {
      await page.locator(`#tab-${id}`).click();
      await expect(page.locator(`#panel-${id}`)).toHaveClass(/active/);
    }

    const body = page.locator("#truth-body");
    const toggle = page.locator("#truth-toggle");
    await expect(body).not.toHaveClass(/collapsed/);
    await toggle.click();
    await expect(body).toHaveClass(/collapsed/);
    await toggle.click();
    await expect(body).not.toHaveClass(/collapsed/);

    await page.locator("#tab-portfolio").click();
    await page.locator("#p-sym").fill("");
    await page.locator("#p-qty").fill("");
    await page.locator("#p-bp").fill("");
    await page.locator(".btn-add").click();
    await expect(page.locator("#perr")).toContainText("Fill in symbol");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator(".logo")).toContainText("GHOST PROTOCOL");
    await expect(page.locator("#panel-crypto")).toHaveClass(/active/);

    expect(consoleErrors, `console errors: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, JSON.stringify(failedApiResponses)).toHaveLength(0);
  });

  test("POST /api/portfolio rejects empty symbol", async ({ request }) => {
    const r = await request.post("/api/portfolio", {
      data: {
        symbol: "",
        asset_type: "stock",
        quantity: 1,
        buy_price: 10,
        buy_date: "2026-01-01",
      },
    });
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(j.ok).toBe(false);
    expect(String(j.error || "").length).toBeGreaterThan(0);
  });
});
