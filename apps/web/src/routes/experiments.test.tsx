import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { env } from "@/env";
import { createHandlers } from "@/mocks/handlers";
import { EXP_GEO } from "@/mocks/fixtures";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => server.resetHandlers(...createHandlers()));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Experiment page (v0.14)", () => {
  it("goes back to the list with a toast after it deletes the open experiment", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(`/experiments/${EXP_GEO}`);
    await user.click(await screen.findByRole("button", { name: /^Delete$/ }, { timeout: 10_000 }));
    // the runs list refetches slower than the experiment itself (as against a real server)
    server.use(
      http.get(`${env.apiBase}/v1/runs`, async () => {
        await delay(300);
      }),
    );
    await user.click(await screen.findByRole("button", { name: /Delete experiment/ }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/experiments"), { timeout: 5_000 });
    expect(await screen.findByText("The app deleted the experiment")).toBeTruthy();
    expect(screen.queryByText("The app could not load this experiment")).toBeNull();
  });
});
