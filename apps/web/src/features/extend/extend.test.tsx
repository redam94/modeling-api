/**
 * The catalog + extend surfaces (v0.18 WP-W3): the pure helpers (team items into a spec, the
 * catalog tab, the featured card, pack labels, the palette's team list, the preset and steps
 * inputs, a team template's document) and the components that need no server ("Add your own"
 * with its copy button, the diagnostics list rendering plugin text as text).
 */
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { parse } from "yaml";
import { describe, expect, it, vi } from "vitest";

import { AddYourOwn, defaultPluginKind, noCodeOptions } from "@/components/extend/add-your-own";
import { featuredAbout } from "@/features/about/catalog-browser";
import type { CatalogAbout } from "@/features/about/types";
import { packLabel } from "@/features/plots/plot-catalog";
import { slotChoice, teamPaletteItems } from "@/features/spec-builder/palette";
import { stepCallsOf } from "@/features/spec-builder/visual-builder";
import type { LibraryItem } from "@/features/library/types";
import { MOCK_DIAGNOSTICS } from "@/mocks/library";
import { activeCatalogTab } from "@/routes/catalog";
import { presetModelOf } from "@/routes/spec-editor";

import { diagnosticsSummary, diagnosticTone, DiagnosticsList } from "./diagnostics-list";
import { templateDocument } from "./save-template-dialog";
import { itemByline, teamItems } from "./team";
import { applyModelUse, applyPrepUse, stepsOf } from "./team-items";

const SPEC = `apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: demo # keep me
  project: growth
data:
  dataset: dataset://growth/sales@latest
pipeline:
  - { id: log1p, use: core.log1p@^1, with: { vars: [spend] } }
model:
  use: ts.sarimax@^1
  settings: { order: [1, 0, 0] }
`;

function item(over: Partial<LibraryItem>): LibraryItem {
  return {
    id: "lib_01jaf3a9b8c7d6e5f4g3h2j1k0",
    kind: "model_recipe",
    scope: "project",
    project_id: "growth",
    name: "store-sales",
    title: "Store sales",
    summary: "",
    tags: [],
    latest_revision: 1,
    archived: false,
    created_by: "usr_a",
    created_at: "2026-10-20T09:00:00Z",
    updated_at: "2026-10-20T09:00:00Z",
    etag: '"r1"',
    ...over,
  };
}

describe("team items into a spec", () => {
  it("appends a prep preset's steps with unique ids and records its lineage label", () => {
    const out = applyPrepUse(
      SPEC,
      [
        { id: "log1p", use: "core.log1p@^1", with: { vars: ["price"] } },
        { id: "p1_scale", use: "core.mean_scale@^1" },
      ],
      { "mapi.library/prep": "lib_01jaf3a9b8c7d6e5f4g3h2j1k0-r2" },
    );
    expect(out).not.toBeNull();
    const doc = parse(out!) as { pipeline: { id: string; use: string; with?: unknown }[]; metadata: { labels: Record<string, string> } };
    expect(doc.pipeline.map((s) => s.id)).toEqual(["log1p", "log1p_2", "p1_scale"]);
    expect(doc.pipeline[1]).toEqual({ id: "log1p_2", use: "core.log1p@^1", with: { vars: ["price"] } });
    expect(doc.metadata.labels).toEqual({ "mapi.library/prep": "lib_01jaf3a9b8c7d6e5f4g3h2j1k0-r2" });
    expect(out).toContain("# keep me");
  });

  it("replaces the model block with a recipe's, adds its steps, plots and label", () => {
    const fragment = {
      pipeline: [{ id: "t0_standardize", use: "core.standardize@^1", with: { vars: ["price"] } }],
      model: { use: "tabular.declarative_glm@^1", settings: { family: "normal", covariates: ["price"] } },
      outputs: { plots: ["core.ppc"] },
      metadata: { labels: { "mapi.library/model": "lib_x-r1" } },
    };
    const out = applyModelUse(SPEC, fragment, {});
    const doc = parse(out!) as { pipeline: { id: string }[]; model: { use: string; settings: Record<string, unknown> }; outputs: { plots: string[] }; metadata: { labels: Record<string, string> } };
    expect(doc.model).toEqual({ use: "tabular.declarative_glm@^1", settings: { family: "normal", covariates: ["price"] } });
    expect(doc.pipeline.map((s) => s.id)).toEqual(["log1p", "t0_standardize"]);
    expect(doc.outputs.plots).toEqual(["core.ppc"]);
    expect(doc.metadata.labels["mapi.library/model"]).toBe("lib_x-r1");
  });

  it("skips steps without a use and keeps a YAML that does not parse as null", () => {
    expect(stepsOf([{ id: "a" }, "x", { use: "core.log1p@^1" }])).toEqual([{ id: "core.log1p", use: "core.log1p@^1", with: {} }]);
    expect(stepsOf(null)).toEqual([]);
    expect(applyPrepUse("a: [", [{ id: "x", use: "core.log1p@^1" }])).toBeNull();
  });
});

describe("catalog helpers", () => {
  it("picks the catalog tab from the URL and the local Yours pick", () => {
    expect(activeCatalogTab(undefined, false)).toBe("browse");
    expect(activeCatalogTab(undefined, true)).toBe("yours");
    expect(activeCatalogTab("plots", true)).toBe("plots");
    expect(activeCatalogTab("yours", false)).toBe("yours");
    expect(activeCatalogTab("nope", false)).toBe("browse");
  });

  it("features the marketing-mix model", () => {
    const about = { title: "t", summary_plain: "s", use_when: [], avoid_when: [], data_needs: [], outputs: [], assumptions: [], example: "", difficulty: "beginner", typical_runtime: "minutes" } as unknown as CatalogAbout["about"];
    const a = (kind: CatalogAbout["kind"], id: string) => ({ kind, id, about });
    expect(featuredAbout([a("model", "ts.sarimax"), a("model", "mmm.frequentist"), a("model", "mmm.bayesian")])?.id).toBe("mmm.bayesian");
    expect(featuredAbout([a("model", "ts.sarimax"), a("model", "mmm.frequentist")])?.id).toBe("mmm.frequentist");
    expect(featuredAbout([a("step", "mmm.adstock")])).toBeNull();
  });

  it("names a pack by the app, then by a plugin's pack_label, then by its namespace", () => {
    const plots = [
      { id: "acme.weekly", pack_label: "Acme retail" },
      { id: "lab.x", pack_label: null },
    ];
    expect(packLabel("sci", plots)).toBe("Science");
    expect(packLabel("acme", plots)).toBe("Acme retail");
    expect(packLabel("lab", plots)).toBe("lab");
  });

  it("lists the team's model recipes and prep presets in the palette, matching the search", () => {
    const items = [
      item({ id: "lib_a", title: "Store sales", kind: "model_recipe" }),
      item({ id: "lib_b", title: "Weekly prep", kind: "prep_preset", tags: ["retail"] }),
      item({ id: "lib_c", title: "Fancy chart", kind: "chart_template" }),
      item({ id: "lib_d", title: "Old prep", kind: "prep_preset", archived: true }),
    ];
    expect(teamPaletteItems(items, "").map((i) => i.id)).toEqual(["lib_a", "lib_b"]);
    expect(teamPaletteItems(items, "retail").map((i) => i.id)).toEqual(["lib_b"]);
    expect(slotChoice({ kind: "var_list", label: "x" }, ["a", "b"])).toEqual(["a", "b"]);
    expect(slotChoice({ kind: "var_list", label: "x" }, [])).toBeUndefined();
    expect(slotChoice({ kind: "dim", label: "x" }, "week")).toBe("week");
    expect(slotChoice({ kind: "var", label: "x" }, "")).toBeUndefined();
  });

  it("says who made an item and hides archived ones", () => {
    const now = Date.parse("2026-10-20T11:00:00Z");
    expect(itemByline(item({ created_by: "usr_me" }), "usr_me", now)).toMatch(/^Made by you · updated /);
    expect(itemByline(item({ created_by: "system" }), "usr_me", now)).toMatch(/^Added with the sample project/);
    expect(itemByline(item({}), "usr_me", now)).toMatch(/^Made by your team/);
    expect(teamItems([item({ archived: true }), item({ id: "lib_b" })]).map((i) => i.id)).toEqual(["lib_b"]);
  });

  it("reads the spec's model block and pipeline for the save dialogs", () => {
    expect(presetModelOf({ model: { use: " ts.sarimax@^1 ", settings: { order: [1, 0, 0] } } })).toEqual({ use: "ts.sarimax@^1", settings: { order: [1, 0, 0] } });
    expect(presetModelOf({ model: { use: "" } })).toBeNull();
    expect(presetModelOf(null)).toBeNull();
    expect(stepCallsOf([{ id: "a", use: "core.log1p@^1", with: { vars: ["x"] } }, { use: "core.standardize@^1" }, { id: "no-use" }, 3])).toEqual([
      { id: "a", use: "core.log1p@^1", with: { vars: ["x"] } },
      { id: "step_2", use: "core.standardize@^1" },
    ]);
  });

  it("drops lineage labels from a team template's document, keeps the rest", () => {
    const doc = { apiVersion: "mapi/v1", kind: "Workflow", metadata: { name: "wf", project: "growth", labels: { "workflow-template": "ts_x", "mapi.library/template": "lib_x-r1", team: "a" } }, nodes: [] };
    const out = templateDocument(doc);
    expect(out.metadata).toEqual({ name: "wf", project: "growth", labels: { team: "a" } });
    expect(doc.metadata.labels["workflow-template"]).toBe("ts_x");
    expect(templateDocument({ metadata: { name: "x", project: "p", labels: { "workflow-template": "t" } } }).metadata).toEqual({ name: "x", project: "p" });
  });
});

function inRouter(ui: React.ReactNode) {
  const root = createRootRoute({ component: () => ui });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  return render(<RouterProvider router={router} />);
}

describe("Add your own", () => {
  it("offers the matching builder and the exact plugin command, and copies it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    inRouter(<AddYourOwn kind="plot" />);
    const panel = await screen.findByTestId("add-your-own");
    expect(panel).toHaveAttribute("data-tour", "extend-add-own");
    expect(within(panel).getByRole("link", { name: /Make a chart template/ })).toHaveAttribute("href", expect.stringContaining("/library?"));
    expect(within(panel).getByRole("link", { name: /Make a chart template/ }).getAttribute("href")).toContain("new=chart");
    expect(within(panel).getByTestId("plugin-command")).toHaveTextContent("mapi plugin init acme --kind recipe");
    await user.click(within(panel).getByRole("button", { name: "Workflow node" }));
    expect(within(panel).getByTestId("plugin-command")).toHaveTextContent("mapi plugin init acme --kind node");
    await user.click(within(panel).getByRole("button", { name: "Copy the command" }));
    expect(writeText).toHaveBeenCalledWith("mapi plugin init acme --kind node");
    expect(await within(panel).findByRole("status")).toHaveTextContent("The command is copied.");
    expect(panel.querySelector('[data-tour="extend-no-code"]')).not.toBeNull();
    expect(panel.querySelector('[data-tour="extend-with-code"]')).not.toBeNull();
  });

  it("says so when the browser blocks the copy", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn().mockRejectedValue(new Error("no")) }, configurable: true });
    inRouter(<AddYourOwn kind="model" variant="compact" />);
    await user.click(await screen.findByRole("button", { name: "Copy the command" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(await screen.findByText(/did not allow the copy/)).toBeInTheDocument();
  });

  it("has one no-code option per kind on the Catalog, and the right --kind per panel", () => {
    expect(noCodeOptions("any").map((o) => o.key)).toEqual(["model", "prep", "chart", "workflow"]);
    expect(defaultPluginKind("any")).toBe("model");
    expect(defaultPluginKind("step")).toBe("step");
    expect(defaultPluginKind("workflow_template")).toBe("template");
  });
});

describe("diagnostics", () => {
  it("groups by plugin and shows the plugin's text as text, never as markup", () => {
    const { container } = render(<DiagnosticsList items={MOCK_DIAGNOSTICS} />);
    expect(screen.getAllByTestId("diagnostic")).toHaveLength(3);
    expect(screen.getByRole("region", { name: "Problems from acme-mapi-extras" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Problems from lab-tools" })).toBeInTheDocument();
    const pre = screen.getByText(/ModuleNotFoundError/);
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toContain("<script>alert(1)</script>");
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("Did not load")).toBeInTheDocument();
    expect(diagnosticsSummary(MOCK_DIAGNOSTICS)).toBe("3 problems in 2 sources");
    expect(diagnosticTone("load_error")).toBe("attention");
    expect(diagnosticTone("schema_unsafe")).toBe("warn");
  });
});
