import type { Page } from "@playwright/test";

/** Call before `page.goto`. Inspect arrays in assertions at end of test. */
export function attachStrictClientMonitors(page: Page): {
  consoleErrors: string[];
  pageErrors: string[];
  failedApiResponses: { url: string; status: number }[];
} {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const failedApiResponses: { url: string; status: number }[] = [];

  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => {
    pageErrors.push(`${err.message}\n${err.stack ?? ""}`);
  });
  page.on("response", (response) => {
    try {
      const u = response.url();
      if (!u.includes("/api/")) return;
      const status = response.status();
      if (status >= 400) {
        const path = u.split("?")[0];
        failedApiResponses.push({ url: path, status });
      }
    } catch {
      /* ignore */
    }
  });

  return { consoleErrors, pageErrors, failedApiResponses };
}
