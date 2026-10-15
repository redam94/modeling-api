import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { env } from "@/env";
import { createHandlers } from "@/mocks/handlers";
import { MODEL_ID, modelVersion } from "@/mocks/fixtures";
import { routeTree } from "@/router";

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined; // jsdom: the router restores scroll on navigation
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderAt(url: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [url] }), context: { queryClient } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("ScorePage", () => {
  it("shows a loading state while the model version loads", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/models/:id/versions/:v`, async () => {
        await delay(300);
        return HttpResponse.json(modelVersion);
      }),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    expect(await screen.findByLabelText("Loading")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^Score$/ }, { timeout: 3000 })).toBeInTheDocument();
  });

  it("drops the previous version's results when the version changes", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/models/:id/versions/:v`, ({ params }) =>
        HttpResponse.json({ ...modelVersion, version: Number(params.v), ref: `${modelVersion.ref.replace(/@v\d+$/, "")}@v${String(params.v)}` }),
      ),
    );
    const router = renderAt(`/score?model=${MODEL_ID}&version=1`);
    await userEvent.click(await screen.findByRole("button", { name: "Add" }, { timeout: 3000 }));
    await userEvent.type(screen.getByRole("textbox", { name: "Selection" }), "channel=tv");
    await userEvent.click(screen.getByRole("button", { name: /^Score$/ }));
    const results = await screen.findByRole("region", { name: "Results" });
    await within(results).findByText("ui/plotdata/ppc_fan.arrow", undefined, { timeout: 3000 });

    await router.navigate({ to: "/score", search: { model: MODEL_ID, version: 2 } });
    await waitFor(() => expect(screen.getByText(/@v2$/)).toBeInTheDocument());
    const fresh = screen.getByRole("region", { name: "Results" });
    expect(within(fresh).queryByText("ui/plotdata/ppc_fan.arrow")).toBeNull();
    expect(within(fresh).getByText(/Results appear here/)).toBeInTheDocument();
  });
});
