import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { parse } from "yaml";

import { ApiError } from "@/api/client";
import type { PlotCatalogEntry, PlotSpec } from "@/api/types";
import { env } from "@/env";
import { useScope } from "@/stores/scope";

import { PlotHost } from "./plot-host";
import { pickProperties, RecipePlayground, RecipePlaygroundDialog, recipeYaml, settingErrors, withExampleVariables } from "./recipe-playground";

const PLOT: PlotCatalogEntry = {
  id: "core.ppc_fan",
  version: "1.2.0",
  title: "Fit and forecast",
  settings_schema: {
    type: "object",
    additionalProperties: false,
    properties: {
      columns: { type: "integer", minimum: 1, maximum: 8, default: 3, title: "Columns" },
      height: { type: "integer", minimum: 60, maximum: 2000, default: 180, title: "Height" },
      point: { type: "string", enum: ["median", "mean"], default: "median", title: "Point" },
      title: { anyOf: [{ type: "string", maxLength: 300 }, { type: "null" }], default: null, title: "Title" },
      var: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Var" },
      width: { type: "integer", minimum: 80, maximum: 2000, default: 480, title: "Width" },
    },
  },
};

function plotSpec(title: string, extra: Record<string, unknown> = {}): PlotSpec {
  return {
    id: "core.ppc_fan",
    kind: "vega-lite",
    schema_version: "1",
    provenance: "platform",
    title,
    datasets: {},
    spec: { title, mark: "line", ...extra },
  } as unknown as PlotSpec;
}

const BASE = `${env.apiBase}/v1/catalog/plots`;
let posts: Record<string, unknown>[] = [];
let mode: "ok" | "invalid" | "off" = "ok";

const server = setupServer(
  http.get(`${BASE}/core.ppc_fan/1.2.0/example`, () => HttpResponse.json(plotSpec("Example"))),
  http.post(`${BASE}/:id/:version`, async ({ request }) => {
    const body = (await request.json()) as { settings: Record<string, unknown> };
    posts.push(body.settings);
    if (mode === "off") {
      return HttpResponse.json({ type: "about:blank", title: "Live chart previews are turned off", status: 409, code: "PREVIEW_DISABLED", detail: "Previews are off." }, { status: 409 });
    }
    if (mode === "invalid") {
      return HttpResponse.json(
        {
          type: "about:blank",
          title: "The settings are not valid",
          status: 422,
          code: "SCHEMA_INVALID",
          detail: "Must be at most 8.",
          errors: [{ code: "SCHEMA_INVALID", message: "Must be at most 8.", pointer: "/settings/columns" }],
        },
        { status: 422 },
      );
    }
    return HttpResponse.json({ run_id: "run_1", cached: false, done: true, plot: plotSpec("Drawn", { settings: body.settings }), problem: null });
  }),
);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  posts = [];
  mode = "ok";
  server.resetHandlers();
  useScope.setState({ active: { tenantId: "acme", projectId: "growth" } });
});

function renderPlayground() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const renderSpy = vi.spyOn(PlotHost.prototype, "render");
  const view = render(
    <QueryClientProvider client={qc}>
      <RecipePlayground plot={PLOT} />
    </QueryClientProvider>,
  );
  const lastSpec = () => renderSpy.mock.calls.at(-1)?.[1] as Record<string, unknown> | undefined;
  return { ...view, renderSpy, lastSpec };
}

const status = () => screen.getByTestId("recipe-preview-status");

interface FlowYaml {
  nodes: { with: { recipe: string; settings?: Record<string, unknown> } }[];
}
const flowOf = (text: string) => parse(text) as FlowYaml;

describe("pure helpers", () => {
  it("splits a schema into the instant and the server settings", () => {
    const schema = { ...(PLOT.settings_schema as Record<string, never>), required: ["var", "title"] };
    const quick = pickProperties(schema, (k) => ["title", "width", "height"].includes(k));
    expect(Object.keys(quick.properties as object).sort()).toEqual(["height", "title", "width"]);
    expect(quick.required).toEqual(["title"]);
    expect(quick.additionalProperties).toBe(false);
  });

  it("maps issue pointers to fields", () => {
    const e = new ApiError(422, {
      type: "about:blank",
      title: "Invalid",
      status: 422,
      code: "SCHEMA_INVALID",
      errors: [
        { code: "SCHEMA_INVALID", message: "Must be at most 8.", pointer: "/settings/columns" },
        { code: "SCHEMA_INVALID", message: "Unknown.", pointer: "/var" },
        { code: "SCHEMA_INVALID", message: "Second.", pointer: "/settings/columns" },
        { code: "SCHEMA_INVALID", message: "Broken.", pointer: "settings" },
      ],
    } as never);
    expect(settingErrors(e)).toEqual({ columns: "Must be at most 8.", var: "Unknown." });
    expect(settingErrors(new Error("x"))).toEqual({});
  });

  it("writes YAML that parses back to the spec and workflow snippets with the settings", () => {
    const text = recipeYaml("core.ppc_fan", "1.2.0", { var: "y_obs", title: "Sales: <b>yes</b>", levels: [0.5, 0.9], point: "no", columns: 2 });
    const [specPart, flowPart] = text.split("\n\n");
    expect(parse(specPart!)).toEqual({ outputs: { plots: ["core.ppc_fan@^1"] } });
    expect(parse(flowPart!)).toEqual({
      nodes: [
        {
          id: "ppc_fan",
          use: "plot.render",
          inputs: { model: "fit.model" },
          with: { recipe: "core.ppc_fan@^1", settings: { columns: 2, levels: [0.5, 0.9], point: "no", title: "Sales: <b>yes</b>", var: "y_obs" } },
        },
      ],
    });
    expect(flowOf(recipeYaml("core.ppc_fan", "1.0.0", {})).nodes[0]!.with).toEqual({ recipe: "core.ppc_fan@^1" });
  });
});

describe("RecipePlayground", () => {
  it("shows the example, applies the title at once and sends nothing for it", async () => {
    const user = userEvent.setup();
    const { lastSpec } = renderPlayground();
    expect(screen.getByTestId("recipe-playground")).toHaveAttribute("data-plot", "core.ppc_fan");
    await waitFor(() => expect(lastSpec()?.title).toBe("Example"));
    expect(status()).toHaveTextContent("This is the example chart");
    await user.type(screen.getByLabelText("Title"), "My forecast");
    expect(lastSpec()?.title).toBe("My forecast");
    await user.type(screen.getByLabelText("Width"), "640");
    expect(lastSpec()?.width).toBe(640);
    await act(() => new Promise((r) => setTimeout(r, 900)));
    expect(posts).toEqual([]);
    expect(status()).toHaveAttribute("data-tone", "idle");
    expect(screen.getByTestId("recipe-yaml").textContent).toContain('title: "My forecast"');
  });

  it("sends one debounced request for a server setting, shows a pending state, then the drawn chart", async () => {
    const user = userEvent.setup();
    const { lastSpec } = renderPlayground();
    await waitFor(() => expect(lastSpec()?.title).toBe("Example"));
    await user.type(screen.getByLabelText("Var"), "y_obs");
    // Still inside the debounce: nothing sent, but the page already says it is working.
    expect(posts).toEqual([]);
    expect(status()).toHaveAttribute("data-tone", "busy");
    expect(status()).toHaveTextContent("about 10 seconds");
    expect(status().textContent).not.toMatch(/instant/i);
    await waitFor(() => expect(status()).toHaveAttribute("data-tone", "ok"), { timeout: 3000 });
    expect(posts).toEqual([{ var: "y_obs" }]);
    await waitFor(() => expect(lastSpec()?.title).toBe("Drawn"));
    // The title stays instant on top of the drawn chart, with no new request.
    await user.type(screen.getByLabelText("Title"), "Mine");
    expect(lastSpec()?.title).toBe("Mine");
    await act(() => new Promise((r) => setTimeout(r, 900)));
    expect(posts).toHaveLength(1);
    const flow = flowOf(screen.getByTestId("recipe-yaml").textContent?.split("\n\n")[1] ?? "");
    expect(flow.nodes[0]!.with.settings).toEqual({ title: "Mine", var: "y_obs" });
  });

  it("puts a refused setting's message under its field", async () => {
    mode = "invalid";
    const user = userEvent.setup();
    renderPlayground();
    await user.type(screen.getByLabelText("Columns"), "9");
    await waitFor(() => expect(status()).toHaveAttribute("data-tone", "error"), { timeout: 3000 });
    const field = document.querySelector('[data-field="columns"]') as HTMLElement;
    expect(within(field).getByRole("alert")).toHaveTextContent("Must be at most 8.");
    expect(screen.getByLabelText("Columns")).toHaveAttribute("aria-invalid", "true");
    expect(posts).toEqual([{ columns: 9 }]);
  });

  it("switches to Apply when live previews are off", async () => {
    mode = "off";
    const user = userEvent.setup();
    renderPlayground();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    await user.type(screen.getByLabelText("Var"), "y");
    const apply = await screen.findByRole("button", { name: "Apply" }, { timeout: 3000 });
    expect(posts).toEqual([{ var: "y" }]);
    expect(status()).toHaveTextContent("Live previews are off on this server. Click Apply");
    // In Apply mode, edits wait for the button.
    await user.type(screen.getByLabelText("Var"), "_obs");
    await act(() => new Promise((r) => setTimeout(r, 900)));
    expect(posts).toHaveLength(1);
    mode = "ok";
    fireEvent.click(apply);
    await waitFor(() => expect(status()).toHaveAttribute("data-tone", "ok"));
    expect(posts).toEqual([{ var: "y" }, { var: "y_obs" }]);
  });

  it("says when previews stay off after Apply", async () => {
    mode = "off";
    const user = userEvent.setup();
    renderPlayground();
    await user.type(screen.getByLabelText("Var"), "y");
    fireEvent.click(await screen.findByRole("button", { name: "Apply" }, { timeout: 3000 }));
    await waitFor(() => expect(status()).toHaveTextContent("Your settings are in the YAML below"));
    expect(posts).toEqual([{ var: "y" }, { var: "y" }]);
  });
});

describe("the example's variables (v0.18 live: Var was free text with no hint)", () => {
  it("names them under Var and offers them as suggestions", async () => {
    renderPlayground();
    const input = screen.getByLabelText("Var");
    expect(input.closest("div.grid")).toHaveTextContent("The example has revenue. Leave it empty for the model's main one.");
    const list = document.getElementById(input.getAttribute("list") ?? "");
    expect([...(list?.querySelectorAll("option") ?? [])].map((o) => o.getAttribute("value"))).toEqual(["revenue"]);
  });

  it("keeps a setting's own words and leaves charts without known variables alone", () => {
    const schema = { type: "object", properties: { vars: { type: "array", items: { type: "string" }, description: "Variables to use (default: all)" } } };
    const out = withExampleVariables(schema, "core.decomposition");
    expect((out.schema.properties as Record<string, { description: string }>).vars?.description).toBe(
      "Variables to use (default: all). The example has baseline or channel_contribution. Leave it empty for all of them.",
    );
    expect(out.suggest).toEqual({ vars: ["baseline", "channel_contribution"] });
    expect(withExampleVariables(schema, "acme.weekly")).toEqual({ schema, suggest: {} });
  });
});

describe("RecipePlaygroundDialog", () => {
  it("opens as a labelled dialog around the playground", async () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <RecipePlaygroundDialog plot={PLOT} open onOpenChange={() => undefined} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Fit and forecast" });
    expect(within(dialog).getByTestId("recipe-playground")).toBeInTheDocument();
    expect(within(dialog).getByText("Try it")).toBeInTheDocument();
  });
});
