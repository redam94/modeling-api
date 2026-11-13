/**
 * v0.21: the list pages read `?q=` (the global search's "See all in <Page>" writes it): the
 * search box is filled and the list is filtered. Typing writes the URL with a history replace.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { env } from "@/env";
import * as fx from "@/mocks/fixtures";
import { createHandlers } from "@/mocks/handlers";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const T = { timeout: 10_000 };

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  localStorage.clear();
  server.resetHandlers(...createHandlers());
  // The mock has one model and one spec: a second one of each, so a query can hide a row.
  server.use(
    http.get(`${env.apiBase}/v1/models`, () => HttpResponse.json({ items: [fx.model, { ...fx.model, id: "mdl_01jaf3c1d2e3f4g5h6j7k8m9n1", name: "sales-arima", ref: "model://default/sales-arima" }], next_cursor: null })),
    http.get(`${env.apiBase}/v1/specs`, () => HttpResponse.json({ items: [fx.specDoc, { ...fx.specDoc, id: "spc_01jaf3b1c2d3e4f5g6h7j8k9m1", name: "sales-arima", ref: "spec://default/sales-arima" }], next_cursor: null })),
  );
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

/** [url, name of the search box, name of the table or list, a row the query finds, a row it hides, the count line]. */
const CLIENT_FILTERED: [string, string, string, string, string, RegExp][] = [
  ["/datasets?q=lens_cu", "Search datasets", "Datasets", "lens_cubes", "eu_media_daily", /^1 of \d+ datasets$/],
  ["/models?q=geo", "Search models", "Models", "eu-geo-mmm", "sales-arima", /^\d+ of \d+ models$/],
  ["/build?q=geo", "Search specs", "Specs", "eu-geo-mmm", "sales-arima", /^\d+ of \d+ specs$/],
  ["/workflows?q=unit", "Search workflows", "Workflows", "unit-root-tests", "weekly-readiness", /^1 of \d+ workflows$/],
  ["/dashboards?q=forecast", "Search dashboards", "Dashboards", "Monthly sales forecast", "Weekly media readout", /^1 of \d+ dashboards$/],
];

describe("list pages filter the loaded page by ?q=", () => {
  it.each(CLIENT_FILTERED)("%s lands with the box filled and the list filtered", async (url, boxName, listName, shown, hidden, count) => {
    renderApp(url);
    const box = await screen.findByRole("searchbox", { name: boxName }, T);
    expect(box).toHaveValue(new URL(url, "http://x").searchParams.get("q"));
    const list = await screen.findByRole("table", { name: listName }, T);
    expect(await within(list).findByText(shown, undefined, T)).toBeInTheDocument();
    expect(within(list).queryByText(hidden)).toBeNull();
    expect(screen.getByTestId("list-count")).toHaveTextContent(count);
  });

  it.each(CLIENT_FILTERED)("%s without the query shows the row that the query hides", async (url, boxName, listName, shown, hidden) => {
    renderApp(url.split("?")[0] ?? url);
    const box = await screen.findByRole("searchbox", { name: boxName }, T);
    expect(box).toHaveValue("");
    const list = await screen.findByRole("table", { name: listName }, T);
    expect(await within(list).findByText(hidden, undefined, T)).toBeInTheDocument();
    expect(within(list).getByText(shown)).toBeInTheDocument();
    expect(screen.getByTestId("list-count")).toBeEmptyDOMElement();
  });

  it("writes the typed text to the URL with a replace, and an empty box removes the key", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/datasets");
    const box = await screen.findByRole("searchbox", { name: "Search datasets" }, T);
    const entries = router.history.length;
    await user.type(box, "lens");
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "lens" }), T);
    expect(router.history.length).toBe(entries);
    const list = screen.getByRole("table", { name: "Datasets" });
    await waitFor(() => expect(within(list).queryByText("eu_media_daily")).toBeNull());
    await user.clear(box);
    await waitFor(() => expect(router.state.location.search).toEqual({}), T);
    expect(router.state.location.href).toBe("/datasets");
    expect(router.history.length).toBe(entries);
    expect(await within(list).findByText("eu_media_daily")).toBeInTheDocument();
  });

  it("shows the search glyph, one sentence and a button for a query that finds nothing", async () => {
    const user = userEvent.setup();
    const { router, container } = renderApp("/models?q=zzzz");
    expect(await screen.findByRole("heading", { name: "No models match" }, T)).toBeInTheDocument();
    expect(screen.getByText("The app found no models for “zzzz”.")).toBeInTheDocument();
    expect(container.querySelector("[data-slot='empty-state'] [data-slot='brand-art']")).not.toBeNull();
    expect(screen.queryByRole("table", { name: "Models" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Clear the search" }));
    expect(await screen.findByRole("table", { name: "Models" }, T)).toBeInTheDocument();
    expect(router.state.location.search).toEqual({});
    expect(screen.getByRole("searchbox", { name: "Search models" })).toHaveValue("");
  });

  it("says that it counted the first page only when the list has more pages", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/models`, () =>
        HttpResponse.json({
          items: [{ id: "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", name: "eu-geo-mmm", ref: "model://default/eu-geo-mmm", latest_version: 1, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }],
          next_cursor: "more",
        }),
      ),
    );
    renderApp("/models?q=geo");
    await screen.findByRole("table", { name: "Models" }, T);
    expect(screen.getByTestId("list-count")).toHaveTextContent("1 in the first 1 models");
  });

  it("keeps the status pills of Dashboards in the toolbar, and a pill keeps the query", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/dashboards?q=forecast");
    const bar = await screen.findByRole("search", { name: "Search and filter dashboards" }, T);
    const pills = within(bar).getByRole("radiogroup", { name: "Dashboard status" });
    await user.click(within(pills).getByRole("radio", { name: "Published" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ status: "published", q: "forecast" }), T);
  });

  it("opens the list tab of Workflows for a query in Guided mode", async () => {
    localStorage.setItem("mapi.ui", JSON.stringify({ state: { theme: "light", persona: "guided", personas: {}, sidebarCollapsed: false, navMoreOpen: false }, version: 4 }));
    renderApp("/workflows?q=unit");
    const list = await screen.findByRole("table", { name: "Workflows" }, T);
    expect(await within(list).findByText("unit-root-tests", undefined, T)).toBeInTheDocument();
  });

  it("opens the Endpoints tab of Integrations for a query", async () => {
    renderApp("/integrations?q=zzzz");
    expect(await screen.findByRole("searchbox", { name: "Search endpoints" }, T)).toHaveValue("zzzz");
    expect(await screen.findByRole("heading", { name: "No endpoints match" }, T)).toBeInTheDocument();
  });

  it("filters the catalog by the query of the URL", async () => {
    renderApp("/catalog?q=sarimax");
    const box = await screen.findByRole("searchbox", { name: "Search the catalog" }, T);
    expect(box).toHaveValue("sarimax");
    const items = await screen.findByRole("list", { name: "Catalog items" }, T);
    expect(within(items).getAllByRole("listitem").length).toBeGreaterThan(0);
    expect(screen.getByTestId("list-count")).toHaveTextContent(/^\d+ of \d+ catalog items$/);
    // The kind pills and the difficulty select are in the toolbar.
    const bar = screen.getByRole("search", { name: "Search and filter catalog items" });
    expect(within(bar).getByRole("group", { name: "Kind" })).toBeInTheDocument();
    expect(within(bar).getByRole("combobox", { name: "Difficulty" })).toBeInTheDocument();
  });
});

describe("list pages that the server filters send the query to the API", () => {
  it("/experiments?q= sends name= and keeps the Show archived switch in the toolbar", async () => {
    const seen: (string | null)[] = [];
    server.events.on("request:start", ({ request }) => {
      const u = new URL(request.url);
      if (u.pathname === "/v1/experiments") seen.push(u.searchParams.get("name"));
    });
    const user = userEvent.setup();
    const { router } = renderApp("/experiments?q=prior");
    const box = await screen.findByRole("searchbox", { name: "Search experiments" }, T);
    expect(box).toHaveValue("prior");
    const table = await screen.findByRole("table", { name: "Experiments" }, T);
    expect(await within(table).findByText("Prior sensitivity", undefined, T)).toBeInTheDocument();
    await waitFor(() => expect(within(table).queryByText("Geo adstock study")).toBeNull(), T);
    expect(seen).toContain("prior");
    expect(screen.getByTestId("list-count")).toHaveTextContent("1 experiment");
    const bar = screen.getByRole("search", { name: "Search and filter experiments" });
    await user.click(within(bar).getByRole("switch", { name: "Show archived experiments" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ archived: true, q: "prior" }), T);
    server.events.removeAllListeners();
  });

  it("/runs?q= is the name filter of the runs", async () => {
    const seen: (string | null)[] = [];
    server.events.on("request:start", ({ request }) => {
      const u = new URL(request.url);
      if (u.pathname === "/v1/runs") seen.push(u.searchParams.get("name"));
    });
    const user = userEvent.setup();
    const { router } = renderApp("/runs?q=grid");
    const box = await screen.findByRole("searchbox", { name: "Search runs" }, T);
    expect(box).toHaveValue("grid");
    await waitFor(() => expect(seen).toContain("grid"), T);
    // The URL then has the key of the runs table only.
    expect(router.state.location.href).toBe("/runs?name=grid");
    // The filter grammar chips stay.
    expect(screen.getByRole("search", { name: "Search and filter runs" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by state" })).toBeInTheDocument();
    // The next write uses the key of the runs table.
    await user.type(box, "s");
    await waitFor(() => expect(router.state.location.search).toEqual({ name: "grids" }), T);
    server.events.removeAllListeners();
  });

  it("/library?q= sends q= and opens the first tab that has a match", async () => {
    const seen: (string | null)[] = [];
    server.events.on("request:start", ({ request }) => {
      const u = new URL(request.url);
      if (u.pathname === "/v1/library") seen.push(u.searchParams.get("q"));
    });
    renderApp("/library?q=zzzz");
    const box = await screen.findByRole("searchbox", { name: "Search model recipes" }, T);
    expect(box).toHaveValue("zzzz");
    expect(await screen.findByRole("heading", { name: "No model recipes match" }, T)).toBeInTheDocument();
    expect(seen).toContain("zzzz");
    server.events.removeAllListeners();
  });
});
