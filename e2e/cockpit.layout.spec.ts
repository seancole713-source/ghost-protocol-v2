import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "./safe-test";

// Offline layout regression: use the shipped markup, CSS, renderer and handler.
// All requests are blocked; no production API, scheduler or portfolio is started.
const source = readFileSync(resolve(__dirname, "../cockpit.html"), "utf8");
const css = readFileSync(resolve(__dirname, "../static/base.css"), "utf8");
const html = source.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
  .replace('<link rel="stylesheet" href="/static/base.css">', `<style>${css}</style>`);
const toggle = source.slice(source.indexOf("(function initTruthToggle()"),
  source.indexOf("/* ─── Orchestrator"));
const attribution = source.slice(source.indexOf("function _attrDelta"),
  source.indexOf("var _plDays"));

test("truth toggle receives real clicks during and after delayed attribution layout", async ({ page }) => {
  await page.route("**/*", route => route.abort());
  await page.setContent(html);
  await page.addScriptTag({ content: `function escHtml(s) { return String(s); }\n${toggle}\n${attribution}` });
  await page.evaluate(() => {
    const data = {
      feature_attribution: {
        wins: 20, losses: 10,
        features: Array.from({ length: 30 }, (_, i) => ({
          feature: `signal_${i}`, win_avg: 1, loss_avg: 2, delta: -1,
        })),
        by_regime: [{ regime: "risk-on", wins: 20, losses: 10, win_rate_pct: 66 }],
      },
      component_attribution: {
        available: true,
        components: Array.from({ length: 10 }, (_, i) => ({
          component: `component_${i}`, win_avg: 1, loss_avg: 2, delta: -1,
        })),
      },
    };
    let release!: () => void;
    const barrier = new Promise<void>(resolve => { release = resolve; });
    (window as any).releaseAttribution = release;
    (window as any).attributionWaiting = false;
    window.fetch = async () => {
      (window as any).attributionWaiting = true;
      await barrier;
      return new Response(JSON.stringify(data));
    };
    (window as any).loadAttribution();
  });
  const button = page.locator("#truth-toggle");
  const body = page.locator("#truth-body");
  await expect.poll(() => page.evaluate(() => (window as any).attributionWaiting)).toBe(true);
  await expect(page.locator("#attr-components tbody tr")).toHaveCount(0);
  await button.click();
  await expect(button).toHaveAttribute("aria-expanded", "false");
  await expect(body).toBeHidden();
  // Response stays blocked until after the real click and its assertions.
  await expect(page.locator("#attr-components tbody tr")).toHaveCount(0);
  await page.evaluate(() => (window as any).releaseAttribution());
  await expect(page.locator("#attr-components tbody tr")).toHaveCount(10);
  for (let i = 0; i < 3; i++) {
    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "true");
    await expect(body).toBeVisible();
    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await expect(body).toBeHidden();
  }
});
