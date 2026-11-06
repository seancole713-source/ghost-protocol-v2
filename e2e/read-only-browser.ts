import type { APIRequestContext, BrowserContext } from "@playwright/test";

const readMethods = new Set(["GET", "HEAD", "OPTIONS"]);

/** Block browser writes for every URL, including encoded paths and new pages. */
export async function blockLiveMutations(context: BrowserContext): Promise<string[]> {
  const blocked: string[] = [];
  await context.route("**/*", async route => {
    const method = route.request().method().toUpperCase();
    if (readMethods.has(method)) {
      await route.fallback();
      return;
    }
    blocked.push(method);
    await route.abort("blockedbyclient");
  });
  return blocked;
}

/** APIRequestContext bypasses browser routing, so protect its write methods too. */
export function readOnlyRequest(request: APIRequestContext): APIRequestContext {
  return new Proxy(request, {
    get(target, key) {
      if (["post", "put", "patch", "delete"].includes(String(key))) {
        return async () => { throw new Error("Live API mutations are forbidden"); };
      }
      if (key === "fetch") {
        return async (...args: Parameters<APIRequestContext["fetch"]>) => {
          const [url, options] = args;
          const method = (options?.method || (typeof url === "string" ? "GET" : url.method())).toUpperCase();
          if (!readMethods.has(method)) throw new Error("Live API mutations are forbidden");
          return target.fetch(...args);
        };
      }
      const value = Reflect.get(target, key);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
