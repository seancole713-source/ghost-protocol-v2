import type { Page } from "@playwright/test";

/** Live UI checks cannot mutate a portfolio even if auth/validation regresses. */
export async function blockPortfolioMutations(page: Page): Promise<string[]> {
  const blocked: string[] = [];
  await page.route("**/api/portfolio**", async route => {
    const method = route.request().method().toUpperCase();
    if (["GET", "HEAD", "OPTIONS"].includes(method)) {
      await route.fallback();
      return;
    }
    blocked.push(method);
    await route.abort("blockedbyclient");
  });
  return blocked;
}
