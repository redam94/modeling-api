/**
 * The upload wizard puts the bytes of a file once. "Upload and ingest" while the PUT of
 * "Preview structure" is still on the way waits for that PUT: a second PUT to the signed URL
 * fails with HTTP 412 (the URL allows one object, `x-goog-if-generation-match: 0`). Found on a
 * 12.8 MB export (E58) on a dev stack.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { renderApp } from "@/test/render-app";

const DATASET = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
const T = { timeout: 5000 };

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("the upload wizard", () => {
  it("puts the bytes once when Upload and ingest follows Preview structure at once", async () => {
    const puts: string[] = [];
    const stored = new Set<string>();
    server.use(
      http.put("http://127.0.0.1:4443/mock-upload/:id", async ({ params }) => {
        const id = String(params.id);
        puts.push(id);
        await delay(300);
        if (stored.has(id)) return new HttpResponse(null, { status: 412 });
        stored.add(id);
        return HttpResponse.json({ generation: "1" });
      }),
    );
    const versions: unknown[] = [];
    server.events.on("request:start", ({ request }) => {
      if (request.method === "POST" && /\/v1\/datasets\/[^/]+\/versions$/.test(request.url)) versions.push(request.url);
    });
    const { container } = renderApp(`/datasets/${DATASET}?upload=true`);
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBeNull(), T);
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await userEvent.upload(input, new File([new Uint8Array(4096)], "storm.nc", { type: "application/x-netcdf" }));
    await userEvent.click(await screen.findByRole("button", { name: "Preview structure" }, T));
    await userEvent.click(screen.getByRole("button", { name: "Upload and ingest" }));
    await waitFor(() => expect(versions).toHaveLength(1), T);
    expect(puts).toHaveLength(1);
    expect(screen.queryByText(/upload failed with HTTP 412/)).toBeNull();
  });
});
