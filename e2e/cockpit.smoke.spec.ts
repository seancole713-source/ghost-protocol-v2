import { expect, test } from "@playwright/test";

test.describe("Cockpit smoke", () => {
  test("cockpit page renders tabs and title", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") {
        consoleErrors.push(msg.text());
      }
    });

    const response = await page.goto("/cockpit", { waitUntil: "domcontentloaded" });
    expect(response?.ok()).toBeTruthy();

    await expect(page.locator(".logo")).toContainText("GHOST PROTOCOL");
    await expect(page.locator("#tab-crypto")).toBeVisible();
    await expect(page.locator("#tab-stocks")).toBeVisible();
    await expect(page.locator("#tab-portfolio")).toBeVisible();
    await expect(page.locator("#tab-results")).toBeVisible();
    await expect(page.locator("#tab-news")).toBeVisible();

    expect(consoleErrors, `Console errors found: ${consoleErrors.join(" | ")}`).toHaveLength(0);
  });

  test("core API endpoints return valid payloads", async ({ request }) => {
    const stats = await request.get("/api/stats");
    expect(stats.ok()).toBeTruthy();
    const statsBody = await stats.json();
    expect(statsBody.ok).toBeTruthy();
    expect(typeof statsBody.wins).toBe("number");
    expect(typeof statsBody.losses).toBe("number");

    const cockpit = await request.get("/api/cockpit/context");
    expect(cockpit.ok()).toBeTruthy();
    const cockpitBody = await cockpit.json();
    expect(cockpitBody.ok).toBeTruthy();
    expect(cockpitBody.stats.wins).toBe(statsBody.wins);
    expect(cockpitBody.stats.losses).toBe(statsBody.losses);

    const health = await request.get("/health");
    expect(health.ok()).toBeTruthy();
  });
});
