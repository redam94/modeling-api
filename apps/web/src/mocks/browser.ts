import { setupWorker } from "msw/browser";

import { createHandlers } from "./handlers";

/** Start MSW in the browser (`vite --mode mock`); the worker script lives in mock-public/. */
export async function startMockWorker(): Promise<void> {
  const worker = setupWorker(...createHandlers());
  await worker.start({ onUnhandledRequest: "bypass", quiet: true });
}
