/**
 * e2e/cockpit.smoke.spec.ts — Playwright smoke tests for the Ghost Protocol cockpit.
 *
 * Selectors are chosen to be stable across runs:
 *   - Use data-testid attributes where available
 *   - Fall back to structural/semantic selectors (id, role, text)
 *   - Avoid positional selectors (.nth(), :first-child) which break on layout changes
 *   - Avoid broad text matches that appear in multiple elements
 *
 * Run locally:
 *   BASE_URL=https://ghost-protocol-v2-production.up.railway.app npx playwright test e2e/
 */

import { test, expect, Page } from "@playwright/test";

const BASE_URL =
  process.env.BASE_URL ||
  "https://ghost-protocol-v2-production.up.railway.app";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function loadCockpit(page: Page): Promise<void> {
  await page.goto(`${BASE_URL}/cockpit`, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("Cockpit smoke tests", () => {
  test("page loads with correct title", async ({ page }) => {
    await loadCockpit(page);

    // Use the <title> element — unique, deterministic, never duplicated.
    await expect(page).toHaveTitle(/Ghost Protocol/i);
  });

  test("crypto tab is present and active by default", async ({ page }) => {
    await loadCockpit(page);

    // Target the tab by its stable id attribute, not by text content.
    const cryptoTab = page.locator("#tab-crypto");
    await expect(cryptoTab).toBeVisible();
    await expect(cryptoTab).toHaveClass(/active/);
  });

  test("stocks tab is present", async ({ page }) => {
    await loadCockpit(page);

    const stocksTab = page.locator("#tab-stocks");
    await expect(stocksTab).toBeVisible();
  });

  test("portfolio tab is present", async ({ page }) => {
    await loadCockpit(page);

    const portfolioTab = page.locator("#tab-portfolio");
    await expect(portfolioTab).toBeVisible();
  });

  test("results tab is present", async ({ page }) => {
    await loadCockpit(page);

    const resultsTab = page.locator("#tab-results");
    await expect(resultsTab).toBeVisible();
  });

  test("footer contains Ghost Protocol v2 branding", async ({ page }) => {
    await loadCockpit(page);

    // Target the footer element specifically to avoid matching the <title>.
    const footer = page.locator(".footer");
    await expect(footer).toBeVisible();
    await expect(footer).toContainText("Ghost Protocol v2");
  });

  test("switching to stocks tab shows stocks section", async ({ page }) => {
    await loadCockpit(page);

    const stocksTab = page.locator("#tab-stocks");
    await stocksTab.click();
    await expect(stocksTab).toHaveClass(/active/);

    // Crypto tab should no longer be active
    const cryptoTab = page.locator("#tab-crypto");
    await expect(cryptoTab).not.toHaveClass(/active/);
  });

  test("health API returns 200", async ({ request }) => {
    const resp = await request.get(`${BASE_URL}/api/health`);
    expect(resp.status()).toBe(200);

    const body = await resp.json();
    expect(body).toHaveProperty("status");
    expect(["healthy", "degraded", "critical"]).toContain(body.status);
  });

  test("stats API returns valid payload", async ({ request }) => {
    const resp = await request.get(`${BASE_URL}/api/stats`);
    expect(resp.status()).toBe(200);

    const body = await resp.json();
    expect(body.ok).toBe(true);
    expect(typeof body.wins).toBe("number");
    expect(typeof body.losses).toBe("number");
  });
});
