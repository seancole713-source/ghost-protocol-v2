import { test as base, expect } from "@playwright/test";
import { blockLiveMutations, readOnlyRequest } from "./read-only-browser";

/** Every suite uses fresh, unauthenticated, read-only browser/API contexts. */
export const test = base.extend<{ _liveReadOnly: void }>({
  request: async ({ request }, use) => { await use(readOnlyRequest(request)); },
  _liveReadOnly: [async ({ context }, use) => {
    const blocked = await blockLiveMutations(context);
    await use();
    expect(blocked, "live browser mutation attempts are forbidden").toEqual([]);
  }, { auto: true }],
});
export { expect };
