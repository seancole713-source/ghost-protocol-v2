import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";
import { blockPortfolioMutations } from "./read-only-portfolio";

test.describe("Portfolio security", () => {
  test("portfolio GET is auth-gated and cockpit shows locked state", async ({ page, request }) => {
    const { consoleErrors, pageErrors, failedApiResponses } = attachStrictClientMonitors(page);
    const blockedMutations = await blockPortfolioMutations(page);

    const listed = await request.get("/api/portfolio");
    expect(listed.status()).toBe(401);

    // POST/DELETE authorization is exercised only against a disposable app/DB
    // in test_shadow_evidence_ledger_postgres.py, never against live BASE_URL.

    await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#ppositions")).toContainText("Sign in", { timeout: 20_000 });

    expect(blockedMutations, "live portfolio mutations must never be attempted").toEqual([]);
    expect(consoleErrors, `console: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `pageerror: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, JSON.stringify(failedApiResponses)).toHaveLength(0);
  });
});
