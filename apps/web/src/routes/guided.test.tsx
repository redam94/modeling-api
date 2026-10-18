import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { routeTree } from "@/router";

const SAMPLE = readFileSync(resolve(__dirname, "../../e2e/fixtures/guided-geo-mmm.csv"), "utf-8");

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
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

describe("Guided mode", () => {
  it("offers the templates, marketing mix first, and lists nothing in progress", async () => {
    renderAt("/guided");
    const list = await screen.findByRole("list", { name: "Templates" }, { timeout: 3000 });
    const links = within(list).getAllByRole("link");
    expect(links[0]).toHaveAccessibleName(/^Geo marketing mix/);
    expect(links.map((l) => l.getAttribute("href"))).toContain("/guided/new/weekly_forecast");
    expect(await screen.findByText("Nothing in progress yet")).toBeInTheDocument();
  });

  it("profiles a file in the browser and pre-fills the questions", async () => {
    renderAt("/guided/new/geo_mmm");
    const input = await screen.findByLabelText("Choose a data file", undefined, { timeout: 3000 });
    await userEvent.upload(input, new File([SAMPLE], "guided-geo-mmm.csv", { type: "text/csv" }));
    const kpi = await screen.findByLabelText("Which column is the result you want to explain?", undefined, { timeout: 3000 });
    await waitFor(() => expect(kpi).toHaveValue("Revenue (EUR)"));
    expect(screen.getByLabelText("Which column holds the date or week?")).toHaveValue("week");
    // columns another question already uses are not offered again
    expect(within(kpi).getByRole("option", { name: "week (used for: Date)" })).toBeDisabled();
    expect(within(kpi).getByRole("option", { name: "market (used for: Market)" })).toBeDisabled();
    const spend = screen.getByRole("group", { name: /media spend/ });
    expect(within(spend).getByRole("checkbox", { name: /tv_spend/ })).toBeChecked();
    expect(within(spend).queryByRole("checkbox", { name: /Revenue/ })).toBeNull();
    const preview = screen.getByRole("complementary", { name: "Preview" });
    await within(preview).findByText("You answered all the questions that the model needs.", undefined, { timeout: 3000 });
    expect(within(preview).getByRole("table")).toHaveTextContent("2025-01-05");
    await waitFor(() => expect(screen.getByRole("button", { name: "Upload and continue" })).toBeEnabled());

    // a required answer removed: the next step is not offered, and the preview says what is missing
    await userEvent.selectOptions(kpi, "");
    await within(preview).findByText(/Answer these questions: Which column is the result you want to explain/);
    expect(screen.getByRole("button", { name: "Upload and continue" })).toBeDisabled();
  });
});
