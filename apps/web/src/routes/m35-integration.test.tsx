/**
 * How the M3.5 screens fit together (mock API): the workflow run monitor leads to the model it
 * fitted, the Model Explorer to the version's validations and the governance pages, the Data
 * Explorer to the readiness workflow with the data bound, and the navigation of each persona
 * to its own entry points (Guided: Help me choose and Recipes).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { ADMIN_NAV, CHOOSE_NAV, EXPERT_NAV, GUIDED_MODE_NAV, LOCKED_NAV, PROJECT_NAV, RECIPES_NAV, SETTINGS_NAV, WORKFLOWS_NAV } from "@/components/shell/nav";
import { modelVersionRefs, parseModelVersionRef } from "@/features/workflows/model-link";
import { EXTRA_MODEL_IDS } from "@/mocks/explore";
import { DS_ID, MODEL_ID } from "@/mocks/fixtures";
import { VALIDATION_AWAITING } from "@/mocks/governance";
import { createHandlers } from "@/mocks/handlers";
import { CANONICAL_RUN_ID } from "@/mocks/workflows";
import { useUi } from "@/stores/ui";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const T = { timeout: 5000 };

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  useUi.setState({ persona: null, personas: {} });
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("navigation of each persona", () => {
  it("gives Guided users Help me choose and the recipes first, Expert users the workflows", () => {
    expect(GUIDED_MODE_NAV.slice(0, 3).map((n) => n.label)).toEqual(["Guided", "Help me choose", "Recipes"]);
    expect(GUIDED_MODE_NAV).not.toContain(WORKFLOWS_NAV);
    expect(GUIDED_MODE_NAV.filter((n) => n.to === "/workflows")).toEqual([RECIPES_NAV]);
    expect(EXPERT_NAV).toContain(WORKFLOWS_NAV);
    expect(EXPERT_NAV).toContain(CHOOSE_NAV);
    expect(EXPERT_NAV.map((n) => n.to)).toContain("/governance");
    expect(LOCKED_NAV.map((n) => n.to)).toEqual(["/guided", "/choose", "/workflows", "/integrations"]);
  });

  it("keeps every keyboard chord unique", () => {
    const chords = [...EXPERT_NAV, ...PROJECT_NAV, ADMIN_NAV, SETTINGS_NAV].map((n) => n.chord);
    expect(new Set(chords).size).toBe(chords.length);
  });

  it("shows the Guided entries in the sidebar in Guided mode", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    renderApp("/workflows");
    expect(await screen.findByRole("heading", { name: "Workflows" }, T)).toBeInTheDocument();
    const nav = screen.getAllByRole("navigation").find((n) => within(n).queryByRole("link", { name: /Recipes/ }));
    expect(nav).toBeDefined();
    expect(within(nav!).getByRole("link", { name: /Help me choose/ })).toHaveAttribute("href", "/choose");
    expect(within(nav!).queryByRole("link", { name: /^Workflows/ })).toBeNull();
  });
});

describe("from a workflow run to the model it fitted", () => {
  it("reads model-version refs of outputs", () => {
    expect(parseModelVersionRef("model://growth/wf-fit@v3")).toEqual({ project: "growth", name: "wf-fit", version: 3 });
    expect(parseModelVersionRef("model://growth/wf-fit")).toBeNull();
    expect(parseModelVersionRef("model://growth/wf-fit@v0")).toBeNull();
    expect(parseModelVersionRef("dataset://growth/x@v1")).toBeNull();
    expect(
      modelVersionRefs({
        model: { type: "model_version", ref: "model://g/a@v1" },
        best: { type: "model_version", ref: "model://g/a@v1" },
        data: { type: "dataset", ref: "dataset://g/d@v1" },
      }),
    ).toEqual(["model://g/a@v1"]);
  });

  it("links a finished run's model to the Model Explorer", async () => {
    renderApp(`/workflow-runs/${CANONICAL_RUN_ID}`);
    const models = await screen.findByTestId("run-models", undefined, T);
    expect(within(models).getByRole("link", { name: /Explore wf-weekly-readiness-fit v1 in the Model Explorer/ })).toHaveAttribute("href", `/models/${EXTRA_MODEL_IDS.workflowFit}/versions/1/explore`);
    expect(within(models).getByRole("link", { name: /Validation/ })).toHaveAttribute("href", `/models/${EXTRA_MODEL_IDS.workflowFit}/versions/1/validations`);
  });

  it("opens a workflow run from the run page in its monitor", async () => {
    renderApp(`/runs/${CANONICAL_RUN_ID}`);
    expect(await screen.findByRole("link", { name: /Open the run monitor/ }, T)).toHaveAttribute("href", `/workflow-runs/${CANONICAL_RUN_ID}`);
  });
});

describe("the Model Explorer and governance", () => {
  it("shows the validation status and links to the validation and the policies", async () => {
    renderApp(`/models/${MODEL_ID}/versions/1/explore?section=governance`);
    await screen.findByRole("list", { name: "Check results" }, T);
    const badge = screen.getByRole("link", { name: /^Validation: .*Open the validations of this version$/ });
    expect(badge).toHaveAttribute("href", `/models/${MODEL_ID}/versions/1/validations`);
    expect(screen.getByRole("link", { name: /Open this validation/ })).toHaveAttribute("href", expect.stringMatching(/^\/validations\/val_/));
    expect(screen.getByRole("link", { name: /Policies/ })).toHaveAttribute("href", "/governance");
  });

  it("keeps one store of validations: the explorer's evidence is among the version's validations", async () => {
    renderApp(`/models/${MODEL_ID}/versions/1/validations`);
    const view = await screen.findByRole("table", undefined, T);
    await waitFor(() => expect(within(view).getAllByRole("row").length).toBeGreaterThanOrEqual(4));
    expect(within(view).getAllByText("Checks failed").length).toBeGreaterThanOrEqual(2);
  });

  it("leads from a validation to the model behind it", async () => {
    renderApp(`/validations/${VALIDATION_AWAITING}`);
    expect(await screen.findByRole("link", { name: /Explore the model/ }, T)).toHaveAttribute("href", `/models/${MODEL_ID}/versions/1/explore`);
  });
});

describe("the Data Explorer and workflows", () => {
  it("offers the readiness workflow with the data bound", async () => {
    renderApp(`/datasets/${DS_ID}/versions/1/explore`);
    const link = await screen.findByRole("link", { name: /Readiness workflow/ }, T);
    expect(link.getAttribute("href")).toBe("/workflows/new?template=ts_readiness_arimax&data=dataset%3A%2F%2Fdefault%2Feu_media_daily%40v1");
  });

  it("runs a recipe on the data it was opened with", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    const user = userEvent.setup();
    renderApp("/workflows/recipe?template=ts_readiness_arimax&data=dataset://default/eu_media_daily@v1");
    const select = await screen.findByLabelText("Dataset", undefined, T);
    await waitFor(() => expect(select).toHaveValue("dataset://default/eu_media_daily"));
    await waitFor(() => expect(screen.getByLabelText("Version")).toHaveValue("1"));
    await user.selectOptions(select, "");
    expect(select).toHaveValue("");
  });

  it("runs a recipe the project has no workflow for yet: the workflow it just created", async () => {
    // Found on the live stack: the run was started with the id the page was rendered with
    // (none yet), as POST /v1/workflows//runs -> 404 "The recipe did not start".
    useUi.setState({ persona: "guided", personas: {} });
    const posted: string[] = [];
    server.events.on("request:start", ({ request }) => {
      if (request.method === "POST") posted.push(new URL(request.url).pathname);
    });
    const user = userEvent.setup();
    const { router } = renderApp("/workflows/recipe?template=forecast_bakeoff&data=dataset://default/eu_media_daily@v1");
    const select = await screen.findByLabelText("Dataset", undefined, T);
    await waitFor(() => expect(select).toHaveValue("dataset://default/eu_media_daily"));
    await user.click(screen.getByRole("button", { name: "Run this recipe" }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workflow-runs\/run_/), T);
    const created = posted.indexOf("/v1/workflows");
    expect(created).toBeGreaterThanOrEqual(0);
    expect(posted.slice(created + 1).find((path) => path.endsWith("/runs"))).toMatch(/^\/v1\/workflows\/wf_[a-z0-9]+\/runs$/);
    server.events.removeAllListeners("request:start");
  });
});
