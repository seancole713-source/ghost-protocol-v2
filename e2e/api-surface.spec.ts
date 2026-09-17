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
  "/api/schema": 404,         // internal database metadata
  "/api/db-probe": 404,       // internal database diagnostics
  "/api/debug-signal/WOLF": 404, // internal model trace
};

async function getWithRateLimitRetry(request: any, path: string) {
  let last = await request.get(path);
  for (const delayMs of [1_000, 2_500, 5_000, 10_000]) {
    if (last.status() !== 429) return last;
    const retryAfter = Number(last.headers()["retry-after"] || 0);
    await new Promise((resolve) => setTimeout(resolve, Math.max(delayMs, retryAfter * 1000)));
    last = await request.get(path);
  }
  return last;
}

test.describe("API GET surface", () => {
  for (const path of READ_ONLY_JSON_GETS) {
    test(`GET ${path} returns JSON and expected status`, async ({ request }) => {
      const r = await getWithRateLimitRetry(request, path);
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

  test("GET / redirects toward picks (main landing page)", async ({ request }) => {
    const r = await request.get("/", { maxRedirects: 0 });
    expect([301, 302, 303, 307, 308]).toContain(r.status());
    const loc = r.headers()["location"] ?? "";
    expect(loc).toMatch(/picks/i);
  });
});

test("discovery separates current observations from daily history", async ({ request }) => {
  const response = await getWithRateLimitRetry(request, "/api/intelligence/market-movers");
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.alert_version).toBe("discovery_alerts_v2");
  expect(body.selection).toBe("latest_observation_not_largest_historical_move");
  expect(body.decision_eligible).toBe(false);
  expect(Array.isArray(body.historical_alerts)).toBe(true);
  expect(body.discovery_coverage.full_market_coverage).toBe(false);
  for (const row of body.alerts) {
    expect(row.observation_kind).toBe("intraday_observation");
    expect(Number.isFinite(row.move_pct)).toBe(true);
    expect(row.source_ts).toBeGreaterThan(0);
    expect(row.source_age_s).toBeGreaterThanOrEqual(0);
    expect(row.source_age_s).toBeLessThanOrEqual(body.intraday_max_age_s);
    expect(row.decision_eligible).toBe(false);
  }
  for (const row of body.historical_alerts) {
    expect(row.observation_kind).toBe("daily_history");
    expect(row.decision_eligible).toBe(false);
  }
});

test("discovery budget counts unique provider-screen-symbol observations", async ({ request }) => {
  const response = await getWithRateLimitRetry(request, "/api/intelligence/external-discovery?limit=200");
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.selection).toBe("latest_per_provider_screen_symbol");
  const identities = body.items.map((row: any) => `${row.provider}:${row.screen}:${row.symbol}`);
  expect(new Set(identities).size).toBe(identities.length);
  expect(body.available_count).toBeGreaterThanOrEqual(body.count);
  expect(body.limit_truncated).toBeGreaterThanOrEqual(0);
});

test("market sessions never call unknown-age references live", async ({ request }) => {
  const response = await getWithRateLimitRetry(request, "/api/market/sessions?symbols=NOK,MRVL,BBNX&max_fresh=0");
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.fresh_fetches).toBe(0);
  for (const row of Object.values(body.sessions) as any[]) {
    expect(row).toHaveProperty("cache_age_seconds");
    expect(row).toHaveProperty("quote_status");
    if (row.provider_state === "live") {
      expect(row.quote_status).toBe("fresh");
      expect(row.price_as_of_ts).toBeTruthy();
      expect(row.freshness_seconds).toBeGreaterThanOrEqual(0);
      expect(row.freshness_seconds).toBeLessThan(60);
    }
    if (row.quote_status === "reference_only") {
      expect(row.provider_state).toBe("reference_only");
      expect(row.price_as_of_ts).toBeNull();
      expect(row.freshness_seconds).toBeNull();
    }
  }
});
