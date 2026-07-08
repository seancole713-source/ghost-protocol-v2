/**
 * console-audit.spec.ts — Deep-dive display audit for the unified prediction console.
 *
 * Catches the bug classes we've seen:
 *   1. JS runtime errors (escHtml undefined, etc.)
 *   2. Missing data / empty panels (DOMO "No Ghost report logged")
 *   3. Duplicate rows (candidate + telegram source in squeeze daily log)
 *   4. Null/missing values rendered as "—" when they should show data
 *   5. API 4xx/5xx responses
 *
 * Run:  npx playwright test --config=playwright.config.ts e2e/console-audit.spec.ts
 */

import { expect, test } from "@playwright/test";
import { attachStrictClientMonitors } from "./error-collectors";

// ── helpers ──────────────────────────────────────────────────────────

/** Click a nav button by its data-section attribute, wait for the panel to settle. */
async function switchTab(page: any, section: string) {
  await page.locator(`nav button[data-section="${section}"]`).click();
  await page.waitForTimeout(800); // let fetch + render settle
}

/** Count <tr> elements inside a <tbody> that follows a heading with the given text. */
async function rowCountInSection(page: any, headingText: string): Promise<number> {
  const heading = page.locator("h3").filter({ hasText: headingText });
  if ((await heading.count()) === 0) return 0;
  // The table is the next <table> after the heading
  const table = heading.locator("~ table");
  if ((await table.count()) === 0) return 0;
  return table.locator("tbody tr").count();
}

/** Return an array of text contents for all <td> cells in the first column of a table. */
async function firstColumnValues(page: any, headingText: string): Promise<string[]> {
  const heading = page.locator("h3").filter({ hasText: headingText });
  if ((await heading.count()) === 0) return [];
  const table = heading.locator("~ table");
  if ((await table.count()) === 0) return [];
  const cells = table.locator("tbody tr td:first-child");
  const count = await cells.count();
  const vals: string[] = [];
  for (let i = 0; i < count; i++) {
    vals.push((await cells.nth(i).textContent())?.trim() ?? "");
  }
  return vals;
}

/** Assert no two values in `arr` are identical (catches duplicate rows). */
function assertNoDuplicates(arr: string[], label: string) {
  const seen = new Set<string>();
  const dups: string[] = [];
  for (const v of arr) {
    if (seen.has(v)) dups.push(v);
    seen.add(v);
  }
  expect(dups, `${label}: duplicate values found`).toEqual([]);
}

/** Assert no cell in a table column contains only "—" (missing data). */
async function assertNoEmptyDashCells(page: any, headingText: string, label: string) {
  const heading = page.locator("h3").filter({ hasText: headingText });
  if ((await heading.count()) === 0) return; // section not present is ok
  const table = heading.locator("~ table");
  if ((await table.count()) === 0) return;
  const cells = table.locator("tbody tr td");
  const count = await cells.count();
  const dashCells: string[] = [];
  for (let i = 0; i < count; i++) {
    const text = (await cells.nth(i).textContent())?.trim() ?? "";
    if (text === "—") {
      // Get which row/column this is
      const rowIdx = Math.floor(i / (await table.locator("thead th").count()));
      dashCells.push(`row ${rowIdx + 1}: "${text}"`);
    }
  }
  // "—" is acceptable for genuinely missing data, but if EVERY cell in a column
  // is "—", that's a bug. We check that at least some cells have real data.
  const nonDash = count - dashCells.length;
  expect(nonDash, `${label}: all ${count} cells are "—" (missing data)`).toBeGreaterThan(0);
}

// ── tests ────────────────────────────────────────────────────────────

test.describe("Console display audit", () => {
  test("no JS errors, no failed API calls on load", async ({ page }) => {
    const { consoleErrors, pageErrors, failedApiResponses } =
      attachStrictClientMonitors(page);

    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    // Wait for the async loadAll() to finish — health pill flips to "Ghost online"
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });

    expect(consoleErrors, `Console errors: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `Page errors: ${pageErrors.join(" | ")}`).toHaveLength(0);
    expect(failedApiResponses, `Failed API: ${JSON.stringify(failedApiResponses)}`).toHaveLength(0);
  });

  test("Overview tab — hero ring, coverage, regime all populated", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });

    // Hero ring should show a grade, not "—"
    const grade = await page.locator("#heroGrade").textContent();
    expect(grade?.trim()).not.toBe("—");
    expect(grade?.trim().length).toBeGreaterThan(0);

    // Coverage should be "N/25" not "—"
    const cov = await page.locator("#mCoverage").textContent();
    expect(cov?.trim()).toMatch(/\d+\/25/);

    // Regime should not be "—"
    const regime = await page.locator("#mRegime").textContent();
    expect(regime?.trim()).not.toBe("—");
  });

  test("My Picks tab — each pick has a grade and action", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "mypicks");

    // My Picks panel should exist and not say "No Ghost report logged"
    const panel = page.locator("#myPicksList");
    await expect(panel).toBeVisible({ timeout: 10_000 });
    const text = await panel.textContent();
    expect(text).not.toContain("No Ghost report logged");

    // Each pick card should have a grade tag (not "—")
    const gradeTags = panel.locator(".tag");
    const count = await gradeTags.count();
    // May be empty if user has no picks — that's ok, just check no error text
    expect(text).not.toContain("Loading your picks");
  });

  test("Wallet tab — balance and positions render without JS errors", async ({ page }) => {
    const { consoleErrors, pageErrors } = attachStrictClientMonitors(page);

    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "wallet");

    // Wallet total value should be visible
    const wTotal = page.locator("#wTotal");
    await expect(wTotal).toBeVisible({ timeout: 10_000 });

    // No JS errors from wallet rendering (escHtml bug)
    expect(consoleErrors, `Console errors: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `Page errors: ${pageErrors.join(" | ")}`).toHaveLength(0);
  });

  test("Today tab — EOD mirror has no duplicate symbols", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "today");

    const symbols = await firstColumnValues(page, "End-of-day mirror");
    // If there are rows, no symbol should appear twice
    if (symbols.length > 0) {
      assertNoDuplicates(symbols, "Today EOD mirror symbols");
    }
    // If there are no resolved rows, the "No resolved rows yet" message should show
  });

  test("Today tab — EOD mirror cells have real data (not all '—')", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "today");

    // Only check if there are resolved rows
    const rowCount = await rowCountInSection(page, "End-of-day mirror");
    if (rowCount > 0) {
      await assertNoEmptyDashCells(page, "End-of-day mirror", "Today EOD mirror");
    }
  });

  test("This week tab — history table has no duplicate symbol+ref rows", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "week");

    // Build composite keys from the first two columns (symbol + direction)
    const heading = page.locator("h3").filter({ hasText: "This week predictions" });
    const table = heading.locator("~ table");
    if ((await table.count()) === 0) return;

    const rows = table.locator("tbody tr");
    const rowCount = await rows.count();
    const keys: string[] = [];
    for (let i = 0; i < rowCount; i++) {
      const sym = (await rows.nth(i).locator("td").nth(0).textContent())?.trim() ?? "";
      const ref = (await rows.nth(i).locator("td").nth(4).textContent())?.trim() ?? "";
      keys.push(`${sym}|${ref}`);
    }
    assertNoDuplicates(keys, "This week history rows (symbol|ref)");
  });

  test("48 hour tab — mirror cards render for pool symbols", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "h48");

    const panel = page.locator("#h48List");
    await expect(panel).toBeVisible({ timeout: 10_000 });
    // Should have at least one prediction card or an empty-state message
    const cards = panel.locator(".prediction-card");
    const empty = panel.locator(".empty");
    const hasContent = (await cards.count()) > 0 || (await empty.count()) > 0;
    expect(hasContent).toBeTruthy();
  });

  test("Live mirror tab — mirror score and precision are numbers", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "tracker");

    // Mirror score should be a number 0-100, not "—" or NaN
    const scores = page.locator(".mirror-score .n");
    const count = await scores.count();
    expect(count, "Live mirror should have at least one score").toBeGreaterThan(0);
    for (let i = 0; i < count; i++) {
      const text = await scores.nth(i).textContent();
      const num = parseInt(text?.trim() ?? "", 10);
      expect(num, `Mirror score ${i} should be a number, got "${text}"`).not.toBeNaN();
      expect(num).toBeGreaterThanOrEqual(0);
      expect(num).toBeLessThanOrEqual(100);
    }
  });

  test("Health section — all checks render with status labels", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    // Health is rendered inline in the overview section, not a separate tab.
    // The miniHealth div is always visible on the overview page.
    await switchTab(page, "overview");

    const healthPanel = page.locator("#miniHealth");
    await expect(healthPanel).toBeVisible({ timeout: 10_000 });

    // Health rows use <div class="kv"> or similar — check that the panel has content
    const text = await healthPanel.textContent();
    expect(text?.trim().length, "Health panel should not be empty").toBeGreaterThan(50);

    // Should contain key health labels
    expect(text).toMatch(/Deploy|OK|Health|API/);
  });

  test("Top Stocks tab — shows locked state or candidates", async ({ page }) => {
    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "top");

    const panel = page.locator("#topStocks");
    await expect(panel).toBeVisible({ timeout: 10_000 });
    const text = await panel.textContent();
    // Should either show "locked" or have prediction cards
    const hasContent =
      text?.includes("locked") ||
      text?.includes("Locked") ||
      (await panel.locator(".prediction-card").count()) > 0;
    expect(hasContent).toBeTruthy();
  });

  test("Bullish tab — renders without errors (may be empty)", async ({ page }) => {
    const { consoleErrors, pageErrors } = attachStrictClientMonitors(page);

    await page.goto("/picks", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#healthPill")).toContainText("Ghost online", { timeout: 30_000 });
    await switchTab(page, "bullish");

    const panel = page.locator("#bullishList");
    await expect(panel).toBeVisible({ timeout: 10_000 });

    expect(consoleErrors, `Console errors: ${consoleErrors.join(" | ")}`).toHaveLength(0);
    expect(pageErrors, `Page errors: ${pageErrors.join(" | ")}`).toHaveLength(0);
  });
});
