import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import type { ProjectScope } from "@/api/queries";
import type { PlotSpec } from "@/api/types";
import { env } from "@/env";
import { installedExtensions, type CatalogDiagnostic } from "@/features/extend/api";
import {
  datasetNamesOf,
  docOfKind,
  isLibraryItemId,
  issuesAt,
  libraryKindOf,
  slotName,
  splitUseRef,
  type LibraryItem,
  type LibraryRevision,
  type LibraryUseResult,
  type LibraryValidation,
} from "@/features/library/types";
import { applyInstantSettings, requestRecipePreview, serverSettingsKey, splitSettings, type PreviewPhase, type RecipePreviewResult } from "@/features/plots/preview-api";

import { createHandlers, createState } from "./handlers";
import { FIXTURE_DOCS, MOCK_DIAGNOSTICS, MOCK_EXTENSIONS_KEY, MOCK_PREVIEW_MS_KEY, MOCK_PREVIEW_OFF_KEY, mockCatalog } from "./library";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "./tenancy";

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  localStorage.clear();
  server.resetHandlers(...createHandlers(createState()));
});

type Who = keyof typeof PERSONAS;

async function call<T>(who: Who, method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ status: number; data: T; headers: Headers }> {
  localStorage.setItem(MOCK_SESSION_KEY, who);
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-Mapi-Project": "acme/growth", "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!), ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, data: (text ? JSON.parse(text) : null) as T, headers: res.headers };
}

const list = async (q = "", who: Who = "ana", project = "acme/growth") =>
  (await call<{ items: LibraryItem[] }>(who, "GET", `/v1/library${q}`, undefined, { "X-Mapi-Project": project })).data.items;

const byName = async (name: string) => (await list("?include_archived=true")).find((i) => i.name === name)!;

describe("library fixtures (mock)", () => {
  it("hold all four kinds, project and tenant items, and hide archived ones by default", async () => {
    const items = await list();
    expect(new Set(items.map((i) => i.kind))).toEqual(new Set(["model_recipe", "prep_preset", "chart_template", "workflow_template"]));
    expect(items.every((i) => isLibraryItemId(i.id))).toBe(true);
    expect(items.find((i) => i.kind === "workflow_template")?.scope).toBe("tenant");
    expect(items.some((i) => i.archived)).toBe(false);
    expect((await list("?include_archived=true")).some((i) => i.archived)).toBe(true);
    expect((await list("?kind=prep_preset")).every((i) => i.kind === "prep_preset")).toBe(true);
    expect((await list("?q=SARIMAX")).map((i) => i.name)).toEqual(["sarimax-weekly"]);
    // another project of the tenant sees only the tenant items
    expect((await list("", "ana", "acme/retail")).map((i) => i.scope)).toEqual(["tenant"]);
  });

  it("keeps user text verbatim and flags an item whose step is missing", async () => {
    const prep = await byName("weekly-totals-log");
    expect(prep.summary).toContain("<b>Markup here is text.</b>");
    const broken = await byName("cap-outliers");
    const rev = await call<LibraryRevision>("ana", "GET", `/v1/library/${broken.id}/revisions/1`);
    expect(rev.data.valid).toBe(false);
    expect(rev.data.issues?.[0]?.pointer).toBe("/steps/0/use");
  });

  it("gives every fixture document a kind its guard accepts", () => {
    for (const doc of Object.values(FIXTURE_DOCS)) expect(libraryKindOf(doc)).not.toBeNull();
    expect(docOfKind("prep_preset", FIXTURE_DOCS.model)).toBeNull();
    expect(libraryKindOf({ kind: "ModelRecipe" })).toBeNull();
  });
});

describe("library writes (mock)", () => {
  it("creates, revises with If-Match and refuses a stale one", async () => {
    const doc = { ...FIXTURE_DOCS.model, metadata: { ...FIXTURE_DOCS.model.metadata, name: "copy-of-revenue" } };
    const created = await call<LibraryItem>("ana", "POST", "/v1/library", { document: doc, message: "mine" });
    expect(created.status).toBe(201);
    expect(created.data.etag).toBe('"r1"');
    expect(created.headers.get("ETag")).toBe('"r1"');
    const id = created.data.id;
    const r2 = await call<LibraryRevision>("ana", "POST", `/v1/library/${id}/revisions`, { document: doc, message: "again" }, { "If-Match": '"r1"' });
    expect(r2.status).toBe(201);
    expect(r2.data.revision).toBe(2);
    expect(r2.data.compiled).toMatchObject({ model: { use: "tabular.declarative_glm@^1" }, metadata: { labels: { "mapi.library/model": `${id}-r2` } } });
    expect((await call("ana", "POST", `/v1/library/${id}/revisions`, { document: doc }, { "If-Match": '"r1"' })).status).toBe(412);
    // the kind cannot change
    expect((await call("ana", "POST", `/v1/library/${id}/revisions`, { document: FIXTURE_DOCS.prep })).status).toBe(422);
    // the name is unique per kind in its scope
    expect((await call("ana", "POST", "/v1/library", { document: doc })).status).toBe(409);
  });

  it("parses YAML text and stores an invalid model recipe with its issues", async () => {
    const text = "apiVersion: mapi/v1\nkind: ModelRecipe\nmetadata: {name: bad-one, title: Bad}\ndeclarative:\n  outcome: {var: revenue_local}\n  terms:\n    - {var: spend_local, prior: {sigma: -1}}\n";
    const created = await call<LibraryItem>("ana", "POST", "/v1/library", { text });
    expect(created.status).toBe(201);
    const rev = await call<LibraryRevision>("ana", "GET", `/v1/library/${created.data.id}/revisions/1`);
    expect(rev.data.valid).toBe(false);
    expect(issuesAt(rev.data.issues, "/declarative/terms/0")[0]?.pointer).toBe("/declarative/terms/0/prior/sigma");
    expect((await call("ana", "POST", "/v1/library", { text: "a: [" })).status).toBe(400);
  });

  it("refuses an invalid workflow template and tenant items from non-admins", async () => {
    const wf = { apiVersion: "mapi/v1", kind: "WorkflowTemplate", metadata: { name: "x", title: "X" }, document: { kind: "Nope" } };
    expect((await call("ana", "POST", "/v1/library", { document: wf })).status).toBe(422);
    const doc = { ...FIXTURE_DOCS.prep, metadata: { ...FIXTURE_DOCS.prep.metadata, name: "rui-prep" } };
    expect((await call("rui", "POST", "/v1/library", { document: doc, scope: "tenant" })).status).toBe(403);
    expect((await call("rui", "POST", "/v1/library", { document: doc })).status).toBe(201);
    expect((await call("vic", "POST", "/v1/library", { document: { ...doc, metadata: { ...doc.metadata, name: "vic" } } })).status).toBe(403);
  });

  it("archives, unarchives and promotes (tenant admins)", async () => {
    const chart = await byName("fit-with-band");
    expect((await call<LibraryItem>("ana", "POST", `/v1/library/${chart.id}:archive`)).data.archived).toBe(true);
    expect((await list()).some((i) => i.id === chart.id)).toBe(false);
    expect((await call<LibraryItem>("ana", "POST", `/v1/library/${chart.id}:unarchive`)).data.archived).toBe(false);
    expect((await call("rui", "POST", `/v1/library/${chart.id}:promote`)).status).toBe(403);
    const promoted = await call<LibraryItem>("ana", "POST", `/v1/library/${chart.id}:promote`);
    expect(promoted.status).toBe(201);
    expect(promoted.data.scope).toBe("tenant");
    expect((await list("", "ana", "acme/retail")).some((i) => i.id === promoted.data.id)).toBe(true);
  });
});

describe("library validate, use and chart preview (mock)", () => {
  it("validates with pointers into the document, and against the dataset", async () => {
    const doc = structuredClone(FIXTURE_DOCS.model) as unknown as { declarative: { terms: { varies_by?: string | null; var: string }[] } };
    doc.declarative.terms[0]!.varies_by = "store";
    doc.declarative.terms[0]!.var = "price";
    const r = await call<LibraryValidation>("ana", "POST", "/v1/library:validate", { document: doc, dataset: "dataset://default/eu_media_daily@v1" });
    expect(r.data.valid).toBe(false);
    const pointers = (r.data.issues ?? []).map((i) => i.pointer);
    expect(pointers).toContain("/declarative/terms/0/varies_by");
    expect(pointers).toContain("/declarative/terms/0/var");
    const ok = await call<LibraryValidation>("ana", "POST", "/v1/library:validate", { document: FIXTURE_DOCS.model });
    expect(ok.data.valid).toBe(true);
    expect(ok.data.compiled).toMatchObject({ pipeline: [{ id: "t0_log1p" }, { id: "t1_standardize" }] });
  });

  it("uses each kind with lineage labels", async () => {
    const model = await byName("revenue-by-market");
    const m = await call<LibraryUseResult>("ana", "POST", `/v1/library/${model.id}:use`, {});
    expect(m.data.spec_fragment).toMatchObject({ model: { use: "tabular.declarative_glm@^1", settings: { covariates: ["spend_local"], groups: ["market"] } } });
    expect(m.data.labels).toEqual({ "mapi.library/model": `${model.id}-r2` });
    const preset = await byName("sarimax-weekly");
    const p = await call<LibraryUseResult>("ana", "POST", `/v1/library/${preset.id}:use`, { exposed: { trend: "ct", maxiter: 5 } });
    expect(p.data.spec_fragment).toMatchObject({ model: { use: "ts.sarimax@^1", settings: { trend: "ct" } } });
    expect((p.data.spec_fragment as { model: { settings: Record<string, unknown> } }).model.settings).not.toHaveProperty("maxiter");
    const prep = await byName("weekly-totals-log");
    const missing = await call("ana", "POST", `/v1/library/${prep.id}:use`, { slots: { time: "date" } });
    expect(missing.status).toBe(422);
    const filled = await call<LibraryUseResult>("ana", "POST", `/v1/library/${prep.id}:use`, { slots: { time: "date", skewed: ["revenue_local", "spend_local"] } });
    expect(filled.data.pipeline).toEqual([
      { id: "p_weekly", use: "core.resample@^1", with: { dim: "date", freq: "W-SUN", how: "sum", full_periods_only: true } },
      { id: "p_log", use: "core.log1p@^1", with: { vars: ["revenue_local", "spend_local"] } },
    ]);
    const wf = await byName("readiness-check");
    const w = await call<LibraryUseResult>("ana", "POST", `/v1/library/${wf.id}:use`, {});
    expect(w.data.workflow_document).toMatchObject({ kind: "Workflow", metadata: { project: "growth", labels: { "mapi.library/template": `${wf.id}-r1` } } });
    const chart = await byName("fit-with-band");
    expect((await call("ana", "POST", `/v1/library/${chart.id}:use`, {})).status).toBe(422);
    const c = await call<LibraryUseResult>("ana", "POST", `/v1/library/${chart.id}:use`, { model_id: "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", model_version: 1 });
    expect(c.data.plot?.provenance.author).toBe(`${chart.id}-r1`);
    expect(c.data.plot?.id).toBe("lib.fit-with-band");
  });

  it("previews a chart template on the recipe's example and refuses unknown datasets and URLs", async () => {
    const ok = await call<{ plot: PlotSpec | null; issues: unknown[] }>("ana", "POST", "/v1/library/charts:preview", { document: FIXTURE_DOCS.chart });
    expect(ok.status).toBe(200);
    expect(ok.data.plot?.datasets).toHaveProperty("fan");
    expect(ok.data.plot?.spec).toMatchObject({ data: { name: "fan" }, columns: 2 });
    const bad = structuredClone(FIXTURE_DOCS.chart) as unknown as { plot: Record<string, unknown> };
    bad.plot.data = { name: "secrets" };
    bad.plot.usermeta = { embedOptions: {} };
    const r = await call<{ plot: PlotSpec | null; issues: { pointer?: string }[] }>("ana", "POST", "/v1/library/charts:preview", { document: bad });
    expect(r.data.plot).toBeNull();
    expect(r.data.issues.map((i) => i.pointer)).toEqual(expect.arrayContaining(["/plot", "/plot/usermeta"]));
  });
});

describe("recipe previews (mock + preview-api)", () => {
  const scope: ProjectScope = { key: "acme/growth", tenantId: "acme", projectId: "growth", headers: { "X-Mapi-Project": "acme/growth" } };

  it("waits for the run, then answers from the cache", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    localStorage.setItem(MOCK_PREVIEW_MS_KEY, "20");
    const phases: PreviewPhase[] = [];
    const a = await requestRecipePreview(scope, "core.ppc_fan", "1.0.0", { columns: 2, title: "Mine" }, { onPhase: (p) => phases.push(p) });
    expect(a.done).toBe(true);
    expect(a.cached).toBe(false);
    expect((a.plot?.spec as { columns?: number }).columns).toBe(2);
    expect(phases.at(-1)).toBe("done");
    const b = await requestRecipePreview(scope, "core.ppc_fan", "1.0.0", { columns: 2, title: "Other" });
    expect(b.cached).toBe(true);
  });

  it("polls a 202 until the run is done", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    localStorage.setItem(MOCK_PREVIEW_MS_KEY, "16000"); // longer than Prefer: wait=12 → 202
    const r = await call<RecipePreviewResult>("ana", "POST", "/v1/catalog/plots/core.ppc_fan/1.0.0:preview", { settings: { columns: 1 } }, { Prefer: "wait=1" });
    expect(r.status).toBe(202);
    expect(r.data.run_id).toBeTruthy();
    const poll = await call<RecipePreviewResult>("ana", "GET", `/v1/catalog/plots/previews/${r.data.run_id}`);
    expect(poll.data.done).toBe(false);
  });

  it("refuses bad settings with pointers, and answers 409 when previews are off", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    await expect(requestRecipePreview(scope, "core.ppc_fan", "1.0.0", { columns: 99 })).rejects.toMatchObject({ status: 422 });
    const r = await call<{ errors: { pointer: string }[] }>("ana", "POST", "/v1/catalog/plots/core.ppc_fan/1.0.0:preview", { settings: { nope: 1 } });
    expect(r.data.errors[0]?.pointer).toBe("/settings/nope");
    localStorage.setItem(MOCK_PREVIEW_OFF_KEY, "1");
    await expect(requestRecipePreview(scope, "core.ppc_fan", "1.0.0", { columns: 2 })).rejects.toMatchObject({ code: "PREVIEW_DISABLED" });
  });

  it("limits previews to 20 a minute", async () => {
    localStorage.setItem(MOCK_PREVIEW_MS_KEY, "0");
    let last = 0;
    for (let i = 1; i <= 21; i++) last = (await call("ana", "POST", "/v1/catalog/plots/core.ppc_fan/1.0.0:preview", { settings: { max_facets: i } }, { Prefer: "wait=1" })).status;
    expect(last).toBe(429);
  });

  it("keeps instant settings out of the request", () => {
    expect(splitSettings({ title: "a", width: 300, columns: 2 })).toEqual({ instant: { title: "a", width: 300 }, server: { columns: 2 } });
    expect(serverSettingsKey({ title: "a", columns: 2 })).toBe(serverSettingsKey({ columns: 2, height: 90 }));
    const plot = { id: "x", kind: "vega-lite", title: "T", schema_version: "v6", provenance: { generated_at: "t" }, spec: { title: "T", facet: {}, spec: { width: 100 } } } as PlotSpec;
    const out = applyInstantSettings(plot, { title: "New", width: 10_000, height: 120 });
    expect(out.title).toBe("New");
    expect(out.spec).toMatchObject({ title: "New", spec: { width: 2000, height: 120 } });
    expect(plot.spec).toMatchObject({ spec: { width: 100 } });
  });
});

describe("extensions and diagnostics (mock)", () => {
  it("shows diagnostics to tenant admins only, with untrusted text as data", async () => {
    const r = await call<CatalogDiagnostic[]>("ana", "GET", "/v1/catalog/diagnostics", undefined, {});
    expect(r.status).toBe(200);
    expect(r.data).toEqual(MOCK_DIAGNOSTICS);
    expect((await call("rui", "GET", "/v1/catalog/diagnostics")).status).toBe(403);
  });

  it("lists the team's plugin step only when opted in", () => {
    expect(installedExtensions(mockCatalog())).toEqual([]);
    localStorage.setItem(MOCK_EXTENSIONS_KEY, "1");
    expect(installedExtensions(mockCatalog())).toEqual([expect.objectContaining({ kind: "step", id: "acme.winsorize", provider: "acme-mapi-extras" })]);
  });
});

describe("library type helpers", () => {
  it("reads slots, refs and datasets without regular expressions", () => {
    expect(slotName("$slot:time")).toBe("time");
    expect(slotName("$slot:")).toBeNull();
    expect(slotName(3)).toBeNull();
    expect(splitUseRef("ts.sarimax@^1")).toEqual(["ts.sarimax", "^1"]);
    expect(splitUseRef("core.ppc")).toEqual(["core.ppc", null]);
    expect(datasetNamesOf(FIXTURE_DOCS.chart.plot)).toEqual(["fan"]);
    expect(datasetNamesOf({ layer: [{ data: { name: "a" } }, { data: { name: "b" } }] })).toEqual(["a", "b"]);
    expect(isLibraryItemId("lib_01jaf3q2x8m9v7k5h4g3f2e1d0")).toBe(true);
    expect(isLibraryItemId("lib_01JAF3Q2X8M9V7K5H4G3F2E1D0")).toBe(false);
  });
});

describe("the list for the global search (v0.21)", () => {
  it("answers at most `limit` items and a cursor to the others, with the text filter", async () => {
    type Page = { items: LibraryItem[]; next_cursor: string | null };
    const all = await list();
    expect(all.length).toBeGreaterThan(2);
    const first = (await call<Page>("ana", "GET", "/v1/library?limit=2")).data;
    expect(first.items.map((i) => i.id)).toEqual(all.slice(0, 2).map((i) => i.id));
    expect(first.next_cursor).toEqual(expect.any(String));
    const rest = (await call<Page>("ana", "GET", `/v1/library?limit=200&cursor=${encodeURIComponent(first.next_cursor ?? "")}`)).data;
    expect(rest.items.map((i) => i.id)).toEqual(all.slice(2).map((i) => i.id));
    expect(rest.next_cursor).toBeNull();
    const word = all[0]!.title.split(" ")[0]!.toLowerCase();
    const found = (await call<Page>("ana", "GET", `/v1/library?q=${encodeURIComponent(word)}&limit=1`)).data;
    expect(found.items).toHaveLength(1);
    expect(found.items[0]!.title.toLowerCase()).toContain(word);
  });
});
