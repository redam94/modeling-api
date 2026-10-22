/**
 * Make your own against the mock API (MSW): the item lists, the model builder (keyboard-only
 * drivers, live check with problems at their fields, words summary, save → the item's URL), the
 * prep builder's slots, the chart template builder's debounced live preview, and "Your charts"
 * on a model version. The page is mounted on a small router of its own (the app router
 * registers it in its own work package).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/overlays";
import { WorkspaceGate } from "@/features/auth/gate";
import { validateLibrarySearch } from "@/features/library/search";
import { YourCharts } from "@/features/library/your-charts";
import { createHandlers } from "@/mocks/handlers";
import * as fx from "@/mocks/fixtures";
import { MOCK_SESSION_KEY } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { LibraryPage } from "./library";

const calls: { method: string; path: string; body: unknown }[] = [];
const server = setupServer(...createHandlers());
server.events.on("request:start", ({ request }) => {
  const url = new URL(request.url);
  if (!url.pathname.includes("/v1/")) return;
  void request
    .clone()
    .text()
    .then((t) => calls.push({ method: request.method, path: url.pathname, body: t ? (JSON.parse(t) as unknown) : null }))
    .catch(() => calls.push({ method: request.method, path: url.pathname, body: null }));
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});
afterEach(() => {
  server.resetHandlers();
  calls.length = 0;
});
afterAll(() => server.close());

function renderPage(url: string, persona = "ana") {
  localStorage.setItem(MOCK_SESSION_KEY, persona);
  useAuth.setState({ mode: "unknown", expired: false, dismissed: false });
  useScope.setState({ active: null, preferred: { tenantId: "acme", projectId: "growth" }, recent: [] });
  const root = createRootRoute({ component: () => <Outlet /> });
  const library = createRoute({ getParentRoute: () => root, path: "/library", component: LibraryPage, validateSearch: validateLibrarySearch });
  const other = createRoute({ getParentRoute: () => root, path: "$", component: () => <p>Another page</p> });
  const router = createRouter({ routeTree: root.addChildren([library, other]), history: createMemoryHistory({ initialEntries: [url] }) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WorkspaceGate>
          <RouterProvider router={router} />
        </WorkspaceGate>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return router;
}

const posts = (suffix: string) => calls.filter((c) => c.method === "POST" && c.path.endsWith(suffix));

describe("Make your own: lists", () => {
  it("shows the project's and the tenant's items by kind, untrusted text as text", async () => {
    renderPage("/library?tab=prep");
    const list = await screen.findByTestId("library-list", undefined, { timeout: 4000 });
    expect(within(list).getByRole("heading", { name: "Weekly totals, then log" })).toBeInTheDocument();
    // The summary holds markup on purpose: it is shown as text, never parsed.
    expect(within(list).getByText(/<b>Markup here is text\.<\/b>/)).toBeInTheDocument();
    expect(list.querySelector("b")).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: /Workflow templates/ }));
    const wf = await screen.findByTestId("library-list");
    expect(within(wf).getByText("Readiness check before a forecast")).toBeInTheDocument();
    expect(within(wf).getByText("Whole tenant")).toBeInTheDocument();
    expect(within(wf).getByRole("button", { name: /Start from this/ })).toBeEnabled();
  });

  it("hides archived items until asked", async () => {
    renderPage("/library?tab=charts");
    const list = await screen.findByTestId("library-list", undefined, { timeout: 4000 });
    expect(within(list).queryByText("Plain fan (old)")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show archived" }));
    await waitFor(() => expect(within(screen.getByTestId("library-list")).getByText("Plain fan (old)")).toBeInTheDocument());
    expect(within(screen.getByTestId("library-list")).getByText("Archived")).toBeInTheDocument();
  });
});

describe("Make your own: model builder", () => {
  it("builds a model with the keyboard only, checks it live and saves it", async () => {
    const user = userEvent.setup();
    const router = renderPage("/library?tab=models&new=model");
    await screen.findByRole("heading", { name: "Describe a model", level: 1 }, { timeout: 4000 });

    // 1. dataset, 2. outcome
    await user.selectOptions(await screen.findByLabelText("Which data will you use it on?"), "dataset://default/eu_media_daily");
    const outcome = await screen.findByLabelText("The outcome");
    await waitFor(() => expect(within(outcome).getByRole("option", { name: /revenue_local/ })).toBeInTheDocument(), { timeout: 4000 });
    await user.selectOptions(outcome, "revenue_local");

    // 3. a driver: the column select and the Add button, by keyboard
    const add = screen.getByLabelText("Add a driver");
    add.focus();
    await user.selectOptions(add, "spend_local");
    await user.tab();
    expect(screen.getByRole("button", { name: /Add a driver/ })).toHaveFocus();
    await user.keyboard("{Enter}");
    const drivers = await screen.findByRole("list", { name: "Drivers" });
    expect(within(drivers).getAllByRole("listitem")).toHaveLength(1);

    // the prior sparkline is live: it says what the prior means
    expect(within(drivers).getByRole("img", { name: /spend_local: Before seeing the data, the effect is most likely between -1\.6\d? and 1\.6\d?\./ })).toBeInTheDocument();
    await user.click(within(drivers).getByRole("button", { name: "Only lifts" }));
    expect(within(drivers).getByRole("img", { name: /It can only lift the outcome/ })).toBeInTheDocument();

    // 4. a group (the dataset's dims)
    await user.click(screen.getByRole("button", { name: "market" }));

    // the words summary follows
    const summary = screen.getByTestId("model-recipe-summary");
    expect(summary).toHaveTextContent("Explains revenue_local, which is a number.");
    expect(summary).toHaveTextContent("One driver: spend_local, standardised, can only lift it.");
    expect(summary).toHaveTextContent("Each market gets its own starting level.");

    // 5. name it, then save from the block
    await user.type(screen.getByLabelText("Give it a name people will recognise"), "Revenue and spend");
    expect(screen.getByLabelText("Short name")).toHaveValue("revenue-and-spend");
    await waitFor(() => expect(posts("/v1/library:validate").length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: "Save model" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ item: expect.stringMatching(/^lib_/) as string }), { timeout: 4000 });
    const created = posts("/v1/library").at(-1)?.body as { document: { declarative: { terms: { var: string; sign: string }[]; groups: { dim: string }[] } } };
    expect(created.document.declarative.terms).toEqual([expect.objectContaining({ var: "spend_local", sign: "positive" })]);
    expect(created.document.declarative.groups).toEqual([{ dim: "market" }]);
    // the builder stays on screen (no reload) and now saves revisions
    expect(await screen.findByRole("button", { name: "Saved" })).toBeDisabled();

    // v0.18 review: back to the list, then open it again: its saved revision, not the blank start
    const id = (router.state.location.search as { item: string }).item;
    act(() => router.history.push("/library?tab=models"));
    await screen.findByTestId("library-list", undefined, { timeout: 4000 });
    act(() => router.history.push(`/library?tab=models&item=${id}`));
    expect(await screen.findByRole("heading", { name: "Revenue and spend", level: 1 }, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByTestId("model-recipe-summary")).toHaveTextContent("One driver: spend_local");
  }, 20_000);

  it("keeps the tour's prior anchor on screen before and after the first driver", async () => {
    const user = userEvent.setup();
    renderPage("/library?tab=models&new=model");
    const container = document.body;
    const add = await screen.findByRole("button", { name: /Add a driver/ }, { timeout: 4000 });
    // v0.18 live: a new recipe has no driver; the "prior" step must still find its anchor
    expect(container.querySelectorAll('[data-tour="library.prior"]')).toHaveLength(1);
    await user.click(add);
    const anchors = container.querySelectorAll('[data-tour="library.prior"]');
    expect(anchors).toHaveLength(1);
    expect(within(anchors[0] as HTMLElement).getByLabelText("Spread")).toBeInTheDocument();
  });

  it("shows the check's problems at the field their pointer names", async () => {
    const user = userEvent.setup();
    renderPage("/library?tab=models&new=model");
    await user.click(await screen.findByRole("button", { name: /Add a driver/ }, { timeout: 4000 }));
    const drivers = screen.getByRole("list", { name: "Drivers" });
    const spread = within(drivers).getByLabelText("Spread");
    await user.clear(spread);
    await user.type(spread, "-2");
    // `/declarative/terms/0/prior/sigma` lands under the Spread field
    const field = spread.closest("div.grid") as HTMLElement;
    await waitFor(() => expect(within(field).getByRole("alert")).toHaveTextContent("The scale must be more than 0"), { timeout: 4000 });
    // and the list of problems moves focus to that field
    const found = screen.getByRole("region", { name: "What the check found" });
    await user.click(within(found).getByRole("button", { name: /The scale must be more than 0/ }));
    await waitFor(() => expect(spread).toHaveFocus());
  });

  it("reorders drivers with the keyboard", async () => {
    const user = userEvent.setup();
    renderPage("/library?tab=models&new=model");
    await user.click(await screen.findByRole("button", { name: /Add a driver/ }, { timeout: 4000 }));
    await user.click(screen.getByRole("button", { name: /Add a driver/ }));
    const drivers = screen.getByRole("list", { name: "Drivers" });
    const [first, second] = within(drivers).getAllByRole("listitem");
    await user.type(within(first!).getByLabelText("Label (optional)"), "Price");
    await user.type(within(second!).getByLabelText("Label (optional)"), "Promo");
    within(drivers).getByRole("button", { name: "Move Promo up" }).focus();
    await user.keyboard("{Enter}");
    expect(within(drivers).getAllByRole("heading").map((h) => h.textContent)).toEqual(["Promo", "Price"]);
    await user.click(within(drivers).getByRole("button", { name: "Remove Promo" }));
    expect(within(drivers).getAllByRole("listitem")).toHaveLength(1);
  });

  it("opens a saved item and shows its revision", async () => {
    renderPage("/library?tab=models");
    const list = await screen.findByTestId("library-list", undefined, { timeout: 4000 });
    await userEvent.click(within(list).getByRole("button", { name: "Open Revenue by market" }));
    expect(await screen.findByRole("heading", { name: "Revenue by market", level: 1 }, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText(/version 2/)).toBeInTheDocument();
    expect(screen.getByTestId("model-recipe-summary")).toHaveTextContent("Media spend (spend_local), logged and standardised, can only lift it, differently in each market.");
    expect(screen.getByLabelText("Short name")).toHaveAttribute("readonly");
  });
});

describe("Make your own: field problems in plain words (v0.18 live)", () => {
  it("shows what to do, not the validator's text", async () => {
    server.use(
      http.post("*/v1/library:validate", () =>
        HttpResponse.json({
          valid: false,
          compiled: null,
          issues: [{ code: "LIBRARY_INVALID", severity: "error", pointer: "/metadata/name", message: "String should match pattern '^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$'" }],
        }),
      ),
    );
    renderPage("/library?tab=models&new=model");
    const name = await screen.findByLabelText("Short name", undefined, { timeout: 4000 });
    const field = name.closest("div.grid") as HTMLElement;
    await waitFor(() => expect(field).toHaveTextContent("Use lower-case letters, digits, - and _, and start and end with a letter or digit."), { timeout: 4000 });
    expect(field).not.toHaveTextContent("String should match pattern");
  });
});

describe("Make your own: prep builder", () => {
  it("adds a step and turns its columns into a slot", async () => {
    const user = userEvent.setup();
    renderPage("/library?tab=prep&new=prep");
    const add = await screen.findByLabelText("Add a step", undefined, { timeout: 4000 });
    await waitFor(() => expect(within(add).getByRole("option", { name: /core\.log1p/ })).toBeInTheDocument());
    await user.selectOptions(add, "core.log1p");
    await user.click(screen.getByRole("button", { name: /Add the step/ }));
    const steps = await screen.findByRole("list", { name: "Steps" });
    await user.click(within(steps).getByRole("button", { name: "vars" }));
    expect(within(steps).getByText(/asked when used/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Name the slots" })).toBeInTheDocument();
    expect(screen.getByLabelText<HTMLInputElement>("Question for vars").value).toContain("which columns (vars)");
  });
});

describe("Make your own: chart template builder", () => {
  it("starts from a chart type and previews it live, debounced", async () => {
    const user = userEvent.setup();
    renderPage("/library?tab=charts&new=chart&source=core.ppc_fan");
    const preview = await screen.findByTestId("chart-preview", undefined, { timeout: 4000 });
    // the first starting point is picked once the example is known, and drawn
    await waitFor(() => expect(within(preview).getByText("Up to date")).toBeInTheDocument(), { timeout: 6000 });
    expect(screen.getByRole("button", { name: /Line with a band/ })).toHaveAttribute("aria-pressed", "true");
    const before = posts("/v1/library/charts:preview").length;
    expect(before).toBeGreaterThan(0);
    // typing a title is many changes, but one preview request
    await user.type(screen.getByLabelText("Chart title"), "Fan by market");
    await waitFor(() => expect(within(preview).getByText("Up to date")).toBeInTheDocument(), { timeout: 4000 });
    await act(() => new Promise((r) => setTimeout(r, 400)));
    expect(posts("/v1/library/charts:preview").length - before).toBe(1);
    const last = posts("/v1/library/charts:preview").at(-1)?.body as { document: { metadata: { title: string }; plot: { data: { name: string } } } };
    expect(last.document.metadata.title).toBe("Fan by market");
    expect(last.document.plot.data.name).toBe("fan");
  }, 20_000);

  it("asks for no preview before a chart type and its spec are there (v0.18 live)", async () => {
    // the debounced copy lagged behind: the first preview went out with no chart type (422)
    const user = userEvent.setup();
    renderPage("/library?tab=charts&new=chart");
    const pick = await screen.findByLabelText("Chart type", undefined, { timeout: 4000 });
    await waitFor(() => expect(within(pick).getByRole("option", { name: /core\.ppc_fan/ })).toBeInTheDocument(), { timeout: 4000 });
    await user.selectOptions(pick, "core.ppc_fan");
    const preview = await screen.findByTestId("chart-preview");
    await waitFor(() => expect(within(preview).getByText("Up to date")).toBeInTheDocument(), { timeout: 6000 });
    const sent = posts("/v1/library/charts:preview").map((c) => (c.body as { document: { source_recipe: string; plot: object } }).document);
    expect(sent.length).toBeGreaterThan(0);
    for (const d of sent) {
      expect(d.source_recipe).toMatch(/^core\.ppc_fan@/);
      expect(Object.keys(d.plot).length).toBeGreaterThan(0);
    }
  }, 15_000);

  it("reports a spec that reads a dataset the chart type does not have", async () => {
    const user = userEvent.setup();
    renderPage("/library?tab=charts&new=chart&source=core.ppc_fan");
    await screen.findByRole("button", { name: /Line with a band/ }, { timeout: 6000 });
    await user.click(screen.getByText("Advanced: Vega-Lite"));
    const json = screen.getByLabelText("Vega-Lite spec as JSON");
    await user.clear(json);
    await user.click(json);
    await user.paste(JSON.stringify({ data: { name: "secrets" }, mark: "line" }));
    await waitFor(() => expect(within(screen.getByRole("region", { name: "What the check found" })).getByText(/reads the dataset "secrets"/)).toBeInTheDocument(), { timeout: 4000 });
  }, 15_000);
});

describe("Your charts", () => {
  it("draws the team's templates whose chart type the version has", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    useAuth.setState({ mode: "unknown", expired: false, dismissed: false });
    useScope.setState({ active: null, preferred: { tenantId: "acme", projectId: "growth" }, recent: [] });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <WorkspaceGate>
          <YourCharts modelId={fx.MODEL_ID} version={1} />
        </WorkspaceGate>
      </QueryClientProvider>,
    );
    const card = await screen.findByRole("region", { name: "Your charts" }, { timeout: 4000 });
    expect(await within(card).findByText("Fit with a 90% band", undefined, { timeout: 4000 })).toBeInTheDocument();
    await waitFor(() => expect(posts(":use").length).toBe(1), { timeout: 4000 });
    expect(posts(":use")[0]!.body).toMatchObject({ model_id: fx.MODEL_ID, model_version: 1 });
  });
});
