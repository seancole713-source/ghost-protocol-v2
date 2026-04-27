import { expect, test } from "@playwright/test";

/**
 * Read-only GET surface: every route the cockpit or ops tooling may hit on load.
 * Skips mutation endpoints and heavy externals (e.g. portfolio price refresh).
 */
const READ_ONLY_JSON_GETS = [
  "/health",
  "/api/health",
  "/api/stats",
  "/api/stats/v32",
  "/api/stats/direction",
  "/api/cockpit/context",
  "/api/picks",
  "/api/history",
  "/api/news",
  "/api/schema",
  "/api/regime",
  "/api/diagnostics",
  "/api/v3/status",
  "/api/coverage",
  "/api/portfolio",
  "/api/v2/recent",
  "/api/db-probe",
  "/api/symbol-accuracy",
  "/api/objective",
  "/api/objective/report?days=7",
  "/api/health/audit/history",
  "/api/price/BTC",
  "/api/debug-signal/BTC",
];

test.describe("API GET surface", () => {
  for (const path of READ_ONLY_JSON_GETS) {
    test(`GET ${path} returns JSON and HTTP OK`, async ({ request }) => {
      const r = await request.get(path);
      expect(r.ok(), `${path} → ${r.status()}`).toBeTruthy();
      const ct = (r.headers()["content-type"] ?? "").toLowerCase();
      expect(ct.includes("json"), `${path} content-type=${ct}`).toBeTruthy();
      const body = await r.json();
      expect(body, `${path} parses to object`).toBeTruthy();
    });
  }

  test("GET /cockpit returns HTML", async ({ request }) => {
    const r = await request.get("/cockpit");
    expect(r.ok()).toBeTruthy();
    const t = await r.text();
    expect(t).toMatch(/GHOST/i);
    expect(t).toMatch(/PROTOCOL/i);
    expect(t.includes("<html")).toBeTruthy();
  });

  test("GET / redirects toward cockpit", async ({ request }) => {
    const r = await request.get("/", { maxRedirects: 0 });
    expect([301, 302, 303, 307, 308]).toContain(r.status());
    const loc = r.headers()["location"] ?? "";
    expect(loc).toMatch(/cockpit/i);
  });
});
