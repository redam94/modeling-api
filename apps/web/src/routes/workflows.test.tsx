import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { CANONICAL_RUN_ID, WORKFLOW_ID } from "@/mocks/workflows";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const seen: { method: string; path: string; body: unknown; headers: Headers }[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    const clone = request.clone();
    void clone.text().then((text) => {
      let body: unknown = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
      seen.push({ method: request.method, path: new URL(request.url).pathname, body, headers: request.headers });
    });
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  seen.length = 0;
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Workflows", () => {
  it("lists the project's workflows and the gallery with About cards", async () => {
    const user = userEvent.setup();
    renderApp("/workflows");
    expect(await screen.findByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "weekly-readiness" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Templates" }));
    const gallery = await screen.findByTestId("template-gallery");
    const cards = within(gallery).getAllByTestId("about-card");
    expect(cards).toHaveLength(4);
    expect(gallery).toHaveTextContent("Time-series readiness to ARIMAX");
    expect(within(cards[0]!).getByText("Use it when")).toBeInTheDocument();
    expect(within(gallery).getAllByRole("link", { name: "Use this template" })[0]).toHaveAttribute("href", expect.stringContaining("/workflows/new?template="));
  });

  it("creates a workflow from a template with its defaults and data bound", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/workflows/new?template=ts_readiness_arimax");
    const name = await screen.findByLabelText("Workflow name");
    // The template's own name is taken by nobody yet, but pick another one.
    await user.clear(name);
    await user.type(name, "sales-readiness");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(await screen.findByLabelText("Max p"), "2");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.selectOptions(await screen.findByLabelText("Your data"), "dataset://default/eu_media_daily");
    await user.click(screen.getByRole("button", { name: "Create workflow" }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workflows\/wf_/));
    const post = seen.find((r) => r.method === "POST" && r.path === "/v1/workflows");
    const doc = (post?.body as { document: { metadata: { name: string; labels: Record<string, string> }; params: Record<string, { default: unknown }>; inputs: Record<string, { default: string }> } }).document;
    expect(doc.metadata.name).toBe("sales-readiness");
    expect(doc.metadata.labels["workflow-template"]).toBe("ts_readiness_arimax");
    expect(doc.params.max_p!.default).toBe(2);
    expect(doc.inputs.data!.default).toBe("dataset://default/eu_media_daily@latest");
  });

  it("starts a run with an idempotency key and the bound data", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(`/workflows/${WORKFLOW_ID}?tab=runs`);
    await user.click(await screen.findByRole("button", { name: "Run workflow" }));
    const dialog = await screen.findByRole("dialog", { name: "Run revision 2" });
    await user.click(within(dialog).getByRole("button", { name: "Start run" }));
    expect(await within(dialog).findByText("Still needed: data.")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Your data"), "dataset://default/eu_media_daily");
    await user.click(within(dialog).getByRole("button", { name: "Start run" }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/workflow-runs\/run_/));
    const post = seen.find((r) => r.method === "POST" && r.path === `/v1/workflows/${WORKFLOW_ID}/runs`);
    expect(post?.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(post?.body).toMatchObject({ revision: 2, inputs: { data: "dataset://default/eu_media_daily@latest" }, use_cache: true });
  });

  it("shows a finished run's steps, verdicts and decisions", async () => {
    const user = userEvent.setup();
    renderApp(`/workflow-runs/${CANONICAL_RUN_ID}`);
    const monitor = await screen.findByTestId("run-monitor");
    expect(within(monitor).getByTestId("run-status")).toHaveTextContent("Succeeded");
    expect(within(monitor).getByTestId("run-summary")).toHaveTextContent("Decisions: Difference? -> then");
    await user.click(screen.getByRole("tab", { name: "Steps" }));
    const steps = screen.getByRole("table", { name: "Steps" });
    expect(within(steps).getByText("branch 'seasonal' took the else path")).toBeInTheDocument();
    await user.click(within(steps).getByRole("button", { name: /Does it wander\? \(ADF\)/ }));
    const drawer = await screen.findByTestId("node-drawer");
    expect(within(drawer).getByTestId("test-verdict")).toHaveTextContent("The series wanders: consider differencing it.");
    expect(within(drawer).getByText("unit root")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("tab", { name: /Decisions/ }));
    const decision = screen.getAllByTestId("branch-decision")[0]!;
    expect(decision).toHaveTextContent("The condition was true, so the then arm was taken; diff ran.");
    expect(decision).toHaveTextContent("nodes.kpss.reject");
    await user.click(screen.getByRole("tab", { name: "Outputs" }));
    expect(await screen.findByTestId("workflow-report")).toHaveTextContent("Time-series readiness and ARIMAX");
  });

  it("previews a schedule and refuses one that fires too often", async () => {
    const user = userEvent.setup();
    renderApp(`/workflows/${WORKFLOW_ID}?tab=automation`);
    await user.click(await screen.findByRole("button", { name: "Schedule" }));
    const form = screen.getByRole("form", { name: "New schedule" });
    const cron = within(form).getByLabelText("Cron expression");
    await user.clear(cron);
    await user.type(cron, "*/5 * * * *");
    expect(within(form).getByText("schedules may fire at most every 15 minutes")).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Add schedule" })).toBeDisabled();
    await user.click(within(form).getByRole("button", { name: "Every Monday at 06:00" }));
    const tz = within(form).getByLabelText("Time zone");
    await user.clear(tz);
    await user.type(tz, "UTC");
    expect(within(form).getByTestId("cron-preview")).toHaveTextContent("Every Monday at 06:00 (UTC)");
    await user.click(within(form).getByRole("button", { name: "Add schedule" }));
    await waitFor(() => expect(seen.some((r) => r.method === "POST" && r.path.endsWith("/schedules"))).toBe(true));
    expect(seen.find((r) => r.method === "POST" && r.path.endsWith("/schedules"))?.body).toMatchObject({ cron: "0 6 * * 1", timezone: "UTC" });
  });
});
