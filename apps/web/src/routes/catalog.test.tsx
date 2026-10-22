/**
 * Catalog (v0.18 WP-W3) against the mock API: the Studio card grid with the featured
 * marketing-mix card, the technical tables, the chart types with "Try it" and "Make a chart
 * template", and the Yours tab (team items, installed plugins, problems for admins only).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { MOCK_EXTENSIONS_KEY } from "@/mocks/library";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  localStorage.removeItem(MOCK_EXTENSIONS_KEY);
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const T = { timeout: 5000 };

describe("Catalog", () => {
  it("browses cards with the featured marketing-mix model, and keeps the technical tables", async () => {
    const user = userEvent.setup();
    renderApp("/catalog");
    expect(await screen.findByRole("heading", { level: 1, name: "Catalog" }, T)).toBeInTheDocument();
    const featured = await screen.findByTestId("catalog-featured", undefined, T);
    expect(within(featured).getByText("Most used")).toBeInTheDocument();
    const items = await screen.findByRole("list", { name: "Catalog items" }, T);
    // The featured model is not listed twice.
    const featuredTitle = within(featured).getByRole("heading", { level: 3 }).textContent ?? "";
    expect(within(items).queryByRole("button", { name: new RegExp(`^${featuredTitle}:`) })).toBeNull();
    // Kind filters keep their names; a filter hides the featured card when it is not a model.
    await user.click(screen.getByRole("button", { name: /^Governance checks/ }));
    expect(screen.queryByTestId("catalog-featured")).toBeNull();
    expect(within(screen.getByRole("list", { name: "Catalog items" })).getAllByRole("listitem").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("add-your-own").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("tab", { name: /Models/ }));
    expect(await screen.findByRole("table", { name: "Model plugins" }, T)).toBeInTheDocument();
  });

  it("gives each chart type Try it and Make a chart template", async () => {
    renderApp("/catalog?tab=plots");
    const cards = await screen.findByRole("list", { name: "Plot recipes" }, T);
    const first = within(cards).getAllByRole("listitem")[0]!;
    expect(within(first).getByTestId("plot-card")).toBeInTheDocument();
    expect(within(first).getByRole("button", { name: /^Try / })).toHaveAttribute("data-tour", "catalog.recipe.try");
    const link = within(first).getByRole("link", { name: /^Make a chart template from / });
    expect(link.getAttribute("href")).toMatch(/\/library\?.*new=chart/);
    expect(link.getAttribute("href")).toContain(`source=${within(first).getByTestId("plot-card").getAttribute("data-plot")}`);
  });

  it("lists your team's items, installed plugins and the problems (admins)", async () => {
    localStorage.setItem(MOCK_EXTENSIONS_KEY, "1");
    const user = userEvent.setup();
    renderApp("/catalog", "ana");
    await user.click(await screen.findByRole("tab", { name: /Yours/ }, T));
    const yours = await screen.findByTestId("catalog-yours", undefined, T);
    const team = await within(yours).findByRole("list", { name: "From your team" }, T);
    expect(within(team).getAllByRole("listitem").length).toBeGreaterThan(0);
    // Markup in a user's summary stays text.
    expect(yours.querySelector("script, b, img")).toBeNull();
    const installed = await within(yours).findByRole("list", { name: "Installed by your team" }, T);
    expect(installed).toHaveTextContent("acme.winsorize");
    expect(installed).toHaveTextContent("Installed from acme-mapi-extras");
    expect(await within(yours).findByTestId("diagnostics-list", undefined, T)).toBeInTheDocument();
    expect(within(yours).getByRole("button", { name: "Describe the catalog again" })).toBeInTheDocument();
  });

  it("hides the problems from people who are not admins", async () => {
    const user = userEvent.setup();
    renderApp("/catalog", "rui");
    await user.click(await screen.findByRole("tab", { name: /Yours/ }, T));
    const yours = await screen.findByTestId("catalog-yours", undefined, T);
    await waitFor(() => expect(within(yours).queryByText("Loading")).toBeNull());
    expect(within(yours).queryByRole("heading", { name: "Problems" })).toBeNull();
    expect(within(yours).queryByTestId("diagnostics-list")).toBeNull();
  });
});
