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

// ------------------------------------------------------------------ the fixes of the v0.21 review

describe("\"Clear the search\" in the empty result", () => {
  it("puts the keyboard focus in the search box of the page", async () => {
    const user = userEvent.setup();
    renderApp("/models?q=zzzz");
    const clear = await screen.findByRole("button", { name: "Clear the search" }, T);
    clear.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("table", { name: "Models" }, T)).toBeInTheDocument();
    const box = screen.getByRole("searchbox", { name: "Search models" });
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
  });
});

/** [url, name of the search box, path of the list in the API, title of the page without things]. */
const WITH_QUERY: [string, string, string, string][] = [
  ["/datasets?q=media", "Search datasets", "/v1/datasets", "No datasets yet"],
  ["/models?q=media", "Search models", "/v1/models", "No models yet"],
  ["/build?q=media", "Search specs", "/v1/specs", "No specs yet"],
  ["/workflows?q=media", "Search workflows", "/v1/workflows", "No workflows yet"],
  ["/integrations?q=media", "Search endpoints", "/v1/endpoints", "This project has no endpoints"],
];

describe("a list page that opens with ?q= always shows its toolbar with the text", () => {
  it.each(WITH_QUERY)("%s: while the list loads", async (url, boxName, apiPath) => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get(`${env.apiBase}${apiPath}`, async () => {
        await held;
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
    );
    renderApp(url);
    const box = await screen.findByRole("searchbox", { name: boxName }, T);
    expect(box).toHaveValue("media");
    // the list has no answer yet: no table and no empty state
    expect(screen.queryByRole("table")).toBeNull();
    expect(document.querySelector("[data-slot='empty-state']")).toBeNull();
    expect(screen.getByTestId("list-count")).toBeEmptyDOMElement();
    release();
  });

  it.each(WITH_QUERY)("%s: when the project has no things of this kind", async (url, boxName, apiPath, emptyTitle) => {
    server.use(http.get(`${env.apiBase}${apiPath}`, () => HttpResponse.json({ items: [], next_cursor: null })));
    renderApp(url);
    expect(await screen.findByRole("heading", { name: emptyTitle }, T)).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: boxName })).toHaveValue("media");
  });

  it("shows the box of the catalog with the text while the cards load", async () => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get(`${env.apiBase}/v1/catalog/abouts`, async () => {
        await held;
        return HttpResponse.json({ items: [], next_cursor: null });
      }),
    );
    renderApp("/catalog?q=media");
    expect(await screen.findByRole("searchbox", { name: "Search the catalog" }, T)).toHaveValue("media");
    expect(screen.queryByRole("list", { name: "Catalog items" })).toBeNull();
    release();
  });

  it("shows no box on a page without things and without a query: the first-time view stays", async () => {
    server.use(http.get(`${env.apiBase}/v1/datasets`, () => HttpResponse.json({ items: [], next_cursor: null })));
    renderApp("/datasets");
    expect(await screen.findByRole("heading", { name: "No datasets yet" }, T)).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).toBeNull();
  });
});

describe("the filters of a list page are in the URL like the text", () => {
  it("keeps the kind pill and the difficulty of the catalog, with a replace, and the default removes the key", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/catalog?q=model");
    const bar = await screen.findByRole("search", { name: "Search and filter catalog items" }, T);
    const kinds = await within(bar).findByRole("group", { name: "Kind" }, T);
    const entries = router.history.length;
    const before = screen.getByTestId("list-count").textContent;
    await user.click(within(kinds).getByRole("button", { name: /^Models/ }));
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "model", kind: "model" }), T);
    expect(within(kinds).getByRole("button", { name: /^Models/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("list-count").textContent).not.toBe(before);
    await user.selectOptions(within(bar).getByRole("combobox", { name: "Difficulty" }), "beginner");
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "model", kind: "model", difficulty: "beginner" }), T);
    expect(router.history.length).toBe(entries);
    await user.click(within(kinds).getByRole("button", { name: /^Everything/ }));
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "model", difficulty: "beginner" }), T);
    await user.selectOptions(within(bar).getByRole("combobox", { name: "Difficulty" }), "all");
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "model" }), T);
    expect(router.state.location.href).toBe("/catalog?q=model");
  });

  it("opens the catalog with the kind and the difficulty of the URL", async () => {
    renderApp("/catalog?kind=model&difficulty=beginner");
    const bar = await screen.findByRole("search", { name: "Search and filter catalog items" }, T);
    const kinds = await within(bar).findByRole("group", { name: "Kind" }, T);
    expect(within(kinds).getByRole("button", { name: /^Models/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(kinds).getByRole("button", { name: /^Everything/ })).toHaveAttribute("aria-pressed", "false");
    expect(within(bar).getByRole("combobox", { name: "Difficulty" })).toHaveValue("beginner");
  });

  it("drops a kind or a difficulty of the URL that is not one", async () => {
    const { router } = renderApp("/catalog?kind=NOPE&difficulty=hard&q=model");
    await screen.findByRole("search", { name: "Search and filter catalog items" }, T);
    await waitFor(() => expect(router.state.location.search).toEqual({ q: "model" }), T);
  });

  it("keeps the Show archived chip of Make your own, with the text", async () => {
    const asked: string[] = [];
    server.events.on("request:start", ({ request }) => {
      const u = new URL(request.url);
      if (u.pathname === "/v1/library" && u.searchParams.has("kind")) asked.push(u.searchParams.get("include_archived") ?? "");
    });
    const user = userEvent.setup();
    const { router } = renderApp("/library?tab=charts");
    const chip = await screen.findByRole("button", { name: "Show archived" }, T);
    expect(chip).toHaveAttribute("aria-pressed", "false");
    const entries = router.history.length;
    await user.click(chip);
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: "charts", archived: true }), T);
    expect(router.history.length).toBe(entries);
    await waitFor(() => expect(asked).toContain("true"), T);
    await user.type(screen.getByRole("searchbox", { name: "Search chart templates" }), "fan");
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: "charts", q: "fan", archived: true }), T);
    await user.click(screen.getByRole("button", { name: "Show archived" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: "charts", q: "fan" }), T);
    server.events.removeAllListeners();
  });

  it("opens Make your own with the chip on for ?archived=true", async () => {
    renderApp("/library?tab=charts&archived=true");
    expect(await screen.findByRole("button", { name: "Show archived" }, T)).toHaveAttribute("aria-pressed", "true");
    expect(await within(await screen.findByTestId("library-list", undefined, T)).findByText("Plain fan (old)", undefined, T)).toBeInTheDocument();
  });
});

describe("a text longer than the API of Make your own takes", () => {
  it("sends the first 120 characters and keeps the full text in the box", async () => {
    const asked: string[] = [];
    server.events.on("request:start", ({ request }) => {
      const u = new URL(request.url);
      if (u.pathname === "/v1/library" && u.searchParams.has("q")) asked.push(u.searchParams.get("q") ?? "");
    });
    const long = "plain fan ".repeat(15).trim();
    expect(long.length).toBeGreaterThan(120);
    renderApp(`/library?tab=charts&q=${encodeURIComponent(long)}`);
    expect(await screen.findByRole("searchbox", { name: "Search chart templates" }, T)).toHaveValue(long);
    await waitFor(() => expect(asked.length).toBeGreaterThan(0), T);
    for (const q of asked) expect(q.length).toBeLessThanOrEqual(120);
    server.events.removeAllListeners();
  });
});
