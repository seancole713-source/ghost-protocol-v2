import { expect, test } from "@playwright/test";

/**
 * Read-only GET surface: every route the cockpit or ops tooling may hit on load.
 * Skips mutation endpoints and heavy externals (e.g. portfolio price refresh).
 *
 * PR #77: /api/diagnostics returns 404 unauthenticated (intentional privacy).
 * /api/portfolio returns 401 unauthenticated (auth-gated). Both are expected.
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
  "/api/v3/status",
  "/api/coverage",
  "/api/v2/recent",
  "/api/db-probe",
  "/api/symbol-accuracy",
  "/api/objective",
  "/api/objective/report?days=7",
  "/api/health/audit/history",
  "/api/price/WOLF",
  "/api/debug-signal/WOLF",
];

// Routes that intentionally return non-200 in production
const EXPECTED_NON_200: Record<string, number> = {
  "/api/diagnostics": 404,   // intentional privacy — returns 404 unauthenticated
  "/api/portfolio": 401,      // auth-gated — requires portfolio auth
};

test.describe("API GET surface", () => {
  for (const path of READ_ONLY_JSON_GETS) {
    test(`GET ${path} returns JSON and expected status`, async ({ request }) => {
      const r = await request.get(path);
      const expectedStatus = EXPECTED_NON_200[path] || 200;
      expect(r.status(), `${path} → ${r.status()} (expected ${expectedStatus})`).toBe(expectedStatus);
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
