/**
 * MSW emulation of uploaded code (v0.19 I-5): `/v1/plugins…` (claim, tickets, the POST to
 * the quarantine bucket, `:complete`, versions, `:request-approval`, `:approve`/`:reject`
 * with the page's digests, `:yank`, usage, recipe examples), the tenant plugin settings and
 * verified uploaders, and the tenant catalog view (`withUploadedEntries`).
 *
 * It is not the server. It applies the rules the pages show: uploads need a tenant admin or
 * an attested editor; public workspaces (Globex) are refused on every route; only a tenant
 * admin other than the uploader may decide, in a session, with the digests the page showed;
 * private versions are visible to their uploader and tenant admins only.
 *
 * Fixtures (Acme, tenant code `k3vq7m`): the plugin `t_k3vq7m_acme` holds a version in every
 * state: approved (Bea), waiting for a reviewer (Bea: Ana may decide; and one by Ana:
 * self-approval is blocked), private kit passed (Ana), tests failed (a pass/fail/skipped kit
 * report), refused (5 inspect problems), rejected, withdrawn and removed. The plugin
 * `t_k3vq7m_geo` holds versions that stay in `uploaded`, `inspecting` and `describing`.
 * Untrusted strings hold markup, bidi controls and a `javascript:` URL on purpose: pages
 * must render them as text.
 *
 * A browser upload moves `inspecting → describing → kit_passed` in about 4 s (a file name
 * that contains `refused` ends in `inspect_failed`, `failing` in `kit_failed`).
 *
 * Switches (localStorage): `mapi.mock.pluginMs` (how long the fake checks take, default
 * 4000), `mapi.mock.pluginsOff=1` (uploads turned off: every route 403),
 * `mapi.mock.devUntrusted=1` (`/v1/me` says uploaded code runs without a sandbox),
 * `mapi.mock.pluginReauth=1` (the next approve/reject answers 401 `REAUTH_REQUIRED` until
 * the user signs in again through the mock identity provider).
 *
 * Imports from `./tenancy` are used inside functions only (tenancy.ts may import
 * `mockPluginFlags` from here).
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { CatalogResponse, Me, ModelCatalogEntry, PlotCatalogEntry, PlotSpec, Problem, StepCatalogEntry } from "@/api/types";
import { env } from "@/env";
import type {
  InspectProblem,
  KitCheck,
  Plugin,
  PluginDecisionRecord,
  PluginExports,
  PluginInspection,
  PluginSbom,
  PluginSettings,
  PluginUsage,
  PluginVersion,
  PluginVersionState,
  PluginVersionSummary,
  VerifiedUploader,
} from "@/features/plugins/types";

import editorGen from "./editor.gen.json";
import { mockPersona, mockProjectRole, PERSONAS } from "./tenancy";

const API = env.apiBase;
const AUTH = env.authBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const ID = "([^/:?]+)";

export const MOCK_PLUGIN_MS_KEY = "mapi.mock.pluginMs";
export const MOCK_PLUGINS_OFF_KEY = "mapi.mock.pluginsOff";
export const MOCK_DEV_UNTRUSTED_KEY = "mapi.mock.devUntrusted";
/** "1": the operator paused uploaded code (`GET /v1/me` `plugins_paused`). */
export const MOCK_PLUGINS_PAUSED_KEY = "mapi.mock.pluginsPaused";
export const MOCK_PLUGIN_REAUTH_KEY = "mapi.mock.pluginReauth";

/** Where the fake quarantine bucket takes POSTs (the GCS shim origin of the other mocks). */
export const MOCK_PLUGIN_BUCKET = "http://127.0.0.1:4443/mock-plugin-upload";

const T0 = Date.parse("2026-10-20T09:00:00Z");
const DAY = 86_400_000;
const MAX_BYTES = 20_971_520;

// ------------------------------------------------------------------------------ people and ids

/** Ana (tenant admin), Rui (runner, attested), and Bea: a second Acme admin who is not a persona. */
export const ANA_ID = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0";
export const RUI_ID = "usr_01jaf3b9c8d7e6f5g4h3j2k1m0";
export const BEA_ID = "usr_01jaf3f9g8h7j6k5m4n3p2b1e0";
const NAMES: Record<string, string> = { [ANA_ID]: "Ana Analyst", [RUI_ID]: "Rui Runner", [BEA_ID]: "Bea Builder" };
/** Tenant admins who are not personas (so a version can have a second admin as uploader). */
const EXTRA_ADMINS: Record<string, string[]> = { acme: [BEA_ID] };

export const ACME_CODE = "k3vq7m";
export const ACME_NS = `t_${ACME_CODE}_acme`;
export const GEO_NS = `t_${ACME_CODE}_geo`;
export const PLUGIN_ACME = "plg_01jbz3q2x8m9v7k5h4g3f2e1a0";
export const PLUGIN_GEO = "plg_01jbz3q2x8m9v7k5h4g3f2e1b0";

const vid = (n: number) => `plv_01jbz3q2x8m9v7k5h4g3f${n.toString(36).padStart(5, "0")}`;
/** Fixture versions by what they show. */
export const V = {
  approved: vid(1),
  awaiting: vid(2),
  awaitingOwn: vid(3),
  privateOwn: vid(4),
  kitFailed: vid(5),
  refused: vid(6),
  rejected: vid(7),
  yanked: vid(8),
  purged: vid(9),
  uploaded: vid(10),
  inspecting: vid(11),
  describing: vid(12),
} as const;

const hex = (seed: string) => {
  let h = 0;
  for (const ch of seed) h = (h * 31 + (ch.codePointAt(0) ?? 0)) >>> 0;
  return h.toString(16).padStart(8, "0").repeat(8);
};
const sha = (seed: string) => `sha256:${hex(seed)}`;
const BASE_IMAGE = sha("base-image-2026-10");

function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // storage unavailable
  }
}

/** `/v1/me` additions (v0.19): read by mocks/tenancy.ts. */
export function mockPluginFlags(): Pick<Me, "plugins_enabled" | "dev_untrusted_code" | "plugins_paused"> {
  const enabled = read(MOCK_PLUGINS_OFF_KEY) !== "1";
  return { plugins_enabled: enabled, dev_untrusted_code: read(MOCK_DEV_UNTRUSTED_KEY) === "1", plugins_paused: enabled && read(MOCK_PLUGINS_PAUSED_KEY) === "1" };
}

function pluginMs(): number {
  const n = Number(read(MOCK_PLUGIN_MS_KEY));
  return Number.isFinite(n) && n >= 0 && read(MOCK_PLUGIN_MS_KEY) !== null ? n : 4000;
}

function problem(status: number, code: Problem["code"], title: string, detail?: string, extra: Partial<Problem> = {}) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [], ...extra } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });

// ------------------------------------------------------------------------------ exports (catalog clones)

const CATALOG = editorGen.catalog as unknown as CatalogResponse;

function stepClone(sourceId: string, id: string, title: string): StepCatalogEntry {
  const src = (CATALOG.steps ?? []).find((s) => s.id === sourceId) ?? (CATALOG.steps ?? [])[0];
  const out = structuredClone(src) as StepCatalogEntry;
  out.id = id;
  out.version = "0.1.0";
  if (out.about) out.about = { ...out.about, title, related: [], references: [] };
  return out;
}

function plotClone(sourceId: string, id: string, title: string): PlotCatalogEntry {
  const src = (CATALOG.plots ?? []).find((p) => p.id === sourceId) ?? (CATALOG.plots ?? [])[0];
  const out = structuredClone(src) as PlotCatalogEntry;
  out.id = id;
  out.version = "0.1.0";
  out.title = title;
  if (out.about) out.about = { ...out.about, title, related: [], references: [] };
  return out;
}

function modelClone(sourceId: string, id: string, title: string, references: { title: string; url: string }[]): ModelCatalogEntry {
  const src = (CATALOG.models ?? []).find((m) => m.manifest.id === sourceId) ?? (CATALOG.models ?? [])[0];
  const out = structuredClone(src) as ModelCatalogEntry;
  out.manifest.id = id;
  out.manifest.version = "0.1.0";
  if (out.manifest.about) out.manifest.about = { ...out.manifest.about, title, related: [], references };
  return out;
}

/** Where each uploaded plot's example comes from (a built-in plot's example). */
const EXAMPLE_SOURCE = "core.fit_vs_actual";

function exportsFor(ns: string, opts: { hostile?: boolean; refs?: string[] } = {}): PluginExports {
  const refs = (opts.refs ?? ["https://example.org/acme"]).map((url, i) => ({ title: `Reference ${i + 1}`, url }));
  return {
    models: [modelClone("ts.ets", `${ns}.store_mean`, "Store mean forecast", refs)],
    steps: [stepClone("core.clip", `${ns}.winsorize`, opts.hostile ? "<img src=x onerror=alert(1)>Winsorize" : "Winsorize")],
    plots: [plotClone(EXAMPLE_SOURCE, `${ns}.store_chart`, opts.hostile ? "Store chart ‮evil‬" : "Store chart")],
    workflow_nodes: [],
    workflow_templates: [],
  };
}

function exportCounts(e: PluginExports | null | undefined): Record<string, number> {
  if (!e) return {};
  const out: Record<string, number> = {};
  const set = (k: string, n: number) => {
    if (n > 0) out[k] = n;
  };
  set("model", e.models?.length ?? 0);
  set("step", e.steps?.length ?? 0);
  set("plot", e.plots?.length ?? 0);
  set("node", e.workflow_nodes?.length ?? 0);
  set("template", e.workflow_templates?.length ?? 0);
  return out;
}

// ------------------------------------------------------------------------------ inspection, kit, SBOM

function sbomFor(ns: string, version: string, files = 40): PluginSbom {
  const dist = `${ns.split("_").join("-")}`;
  const list = Array.from({ length: files }, (_, i) => {
    const path =
      i === files - 4
        ? `${ns}-${version}.dist-info/METADATA`
        : i === files - 3
          ? `${ns}-${version}.dist-info/WHEEL`
          : i === files - 2
            ? `${ns}-${version}.dist-info/entry_points.txt`
            : i === files - 1
              ? `${ns}-${version}.dist-info/RECORD`
              : i === 0
                ? `${ns}/__init__.py`
                : `${ns}/module_${String(i).padStart(2, "0")}.py`;
    return { path, sha256: sha(`${ns}${version}${i}`), size: 600 + ((i * 337) % 4100) };
  });
  return {
    format: "mapi.sbom/v1",
    dist,
    version,
    files: list,
    requires: [
      { name: "numpy", specifier: ">=1.26", marker: null, locked_version: "2.1.3" },
      { name: "pandas", specifier: ">=2.1", marker: null, locked_version: "2.2.3" },
      { name: "scipy", specifier: ">=1.11", marker: "python_version >= '3.12'", locked_version: "1.14.1" },
    ],
    license: "MIT",
    base_image_digest: BASE_IMAGE,
  };
}

function inspectionFor(ns: string, version: string, archive: string, problems: InspectProblem[] = [], files = 40): PluginInspection {
  const sbom = sbomFor(ns, version, files);
  return {
    ok: problems.length === 0,
    archive_sha256: archive,
    dist: sbom.dist,
    version,
    wheel_tags: ["py3-none-any"],
    top_level: [ns],
    members: files,
    uncompressed_bytes: (sbom.files ?? []).reduce((a, f) => a + f.size, 0),
    entry_points: [
      { group: "mapi.models", name: "store_mean", value: `${ns}.models:StoreMean` },
      { group: "mapi.steps", name: "winsorize", value: `${ns}.steps:winsorize` },
      { group: "mapi.plots", name: "store_chart", value: `${ns}.plots:store_chart` },
    ],
    requires_python: ">=3.12",
    sbom: problems.length ? null : sbom,
    problems,
  };
}

const REFUSED_PROBLEMS = (ns: string): InspectProblem[] => [
  { check: "native_code", path: `${ns}/_speedups.cpython-312-x86_64-linux-gnu.so`, message: "The file is compiled code. Only pure Python can be uploaded. Remove the file." },
  { check: "pth_file", path: `${ns}.pth`, message: "Path configuration files (.pth) run code when Python starts. Remove the file." },
  { check: "entry_point_group", path: `${ns}-0.0.9.dist-info/entry_points.txt`, message: "The entry point group mapi.governance_checks is not allowed in an upload." },
  { check: "requirement_unlocked", path: null, message: "requests>=2.99 is not installed in the base image. Use a version the base image has, or remove the requirement." },
  { check: "dist_name", path: null, message: `The distribution name must be ${ns.split("_").join("-")}.` },
];

const PASSING_KIT = (ns: string): KitCheck[] => [
  { entry_id: `${ns}.store_mean`, kind: "model", check: "about_complete", status: "pass", message: "" },
  { entry_id: `${ns}.store_mean`, kind: "model", check: "check_model", status: "pass", message: "" },
  { entry_id: `${ns}.winsorize`, kind: "step", check: "schema_safety", status: "pass", message: "" },
  { entry_id: `${ns}.winsorize`, kind: "step", check: "check_step", status: "pass", message: "" },
  { entry_id: `${ns}.store_chart`, kind: "plot", check: "check_recipe", status: "pass", message: "" },
  { entry_id: `${ns}.store_chart`, kind: "plot", check: "example_render", status: "skipped", message: "No toy data matches this chart, so its data test did not run." },
];

const FAILING_KIT = (ns: string): KitCheck[] => [
  { entry_id: `${ns}.store_mean`, kind: "model", check: "about_complete", status: "pass", message: "" },
  { entry_id: `${ns}.store_mean`, kind: "model", check: "check_model", status: "fail", message: "fit() returned NaN for 3 of 12 predictions on the toy panel." },
  { entry_id: `${ns}.winsorize`, kind: "step", check: "check_step", status: "fail", message: "The step took too long (more than 120 s) on the toy panel.‮ txet neddih" },
  { entry_id: `${ns}.store_chart`, kind: "plot", check: "check_recipe", status: "pass", message: "" },
  { entry_id: `${ns}.store_chart`, kind: "plot", check: "example_render", status: "skipped", message: "No toy data matches this chart, so its data test did not run." },
  { entry_id: null, kind: "package", check: "exports", status: "pass", message: "" },
];

// ------------------------------------------------------------------------------ state

interface Script {
  startedAt: number;
  /** Where the fake checks end. */
  final: "kit_passed" | "awaiting_approval" | "kit_failed" | "inspect_failed";
  durationMs: number;
}

interface VersionRec {
  tenant: string;
  v: PluginVersion;
  /** Present for versions a browser upload made: the state follows the clock. */
  script: Script | null;
  usage: PluginUsage;
}

interface PluginRec {
  tenant: string;
  p: Omit<Plugin, "latest_version" | "versions">;
}

interface Ticket {
  tenant: string;
  pluginId: string;
  filename: string;
  size: number;
  by: string;
  received: number | null;
  used: boolean;
}

export interface PluginsState {
  codes: Map<string, string>;
  plugins: Map<string, PluginRec>;
  versions: Map<string, VersionRec>;
  tickets: Map<string, Ticket>;
  settings: Map<string, { private_use: boolean }>;
  uploaders: Map<string, VerifiedUploader[]>;
  seq: number;
}

function scopeOf(state: PluginVersionState): PluginVersion["scope"] {
  if (state === "approved") return "tenant";
  return state === "kit_passed" || state === "awaiting_approval" ? "private" : "none";
}

interface VersionInit {
  id: string;
  plugin: string;
  ns: string;
  version: string;
  state: PluginVersionState;
  by: string;
  at: number;
  hostile?: boolean;
  problems?: InspectProblem[];
  kit?: KitCheck[] | null;
  approvals?: PluginDecisionRecord[];
  extra?: Partial<PluginVersion>;
  usage?: Partial<PluginUsage>;
}

const URLS_HOSTILE = ["https://example.org/acme", "https://xn--bcher-kva.example/docs", "javascript:alert(1)", "https://user@evil.example/login"];

function makeVersion(o: VersionInit): VersionRec {
  const archive = sha(`${o.id}-archive`);
  const inspected = !["uploaded", "inspecting"].includes(o.state);
  const described = inspected && !["inspect_failed", "describing"].includes(o.state);
  const exports = described ? exportsFor(o.ns, { hostile: o.hostile ?? false, refs: o.hostile ? URLS_HOSTILE : undefined }) : null;
  const kitChecks = described ? (o.kit ?? PASSING_KIT(o.ns)) : null;
  const v: PluginVersion = {
    id: o.id,
    plugin_id: o.plugin,
    version: o.version,
    state: o.state,
    scope: scopeOf(o.state),
    uploaded_by: o.by,
    uploaded_at: iso(o.at),
    overlay_sha256: inspected && o.state !== "inspect_failed" ? archive : null,
    describe_digest: exports ? sha(`${o.id}-describe`) : null,
    exports: exportCounts(exports),
    inspection: inspected ? inspectionFor(o.ns, o.version, archive, o.problems ?? []) : null,
    kit: kitChecks ? { ok: kitChecks.every((c) => c.status !== "fail"), checks: kitChecks } : null,
    exports_detail: exports,
    issues: [],
    base_image_digest: described ? BASE_IMAGE : null,
    approvals: o.approvals ?? [],
    problem: null,
    yank_reason: null,
    yank_note: null,
    purge_after: null,
    urls: exports ? (o.hostile ? URLS_HOSTILE : ["https://example.org/acme"]) : [],
    ...o.extra,
  };
  return {
    tenant: "acme",
    v,
    script: null,
    usage: { runs_30d: 0, model_versions: [], endpoints: [], dashboards: [], truncated: false, ...o.usage },
  };
}

function decision(d: "approve" | "reject", by: string, v: PluginVersion, at: number, note: string | null = null): PluginDecisionRecord {
  return {
    decision: d,
    approver_id: by,
    approver_name: NAMES[by] ?? null,
    note,
    overlay_sha256: v.overlay_sha256 ?? sha("none"),
    describe_digest: v.describe_digest ?? sha("none"),
    base_image_digest: v.base_image_digest ?? BASE_IMAGE,
    decided_at: iso(at),
  };
}

export function createPluginsState(): PluginsState {
  const plugins = new Map<string, PluginRec>([
    [PLUGIN_ACME, { tenant: "acme", p: { id: PLUGIN_ACME, namespace: ACME_NS, short_name: "acme", title: "Acme store tools", created_by: BEA_ID, created_at: iso(T0), updated_at: iso(T0 + 2 * DAY) } }],
    [PLUGIN_GEO, { tenant: "acme", p: { id: PLUGIN_GEO, namespace: GEO_NS, short_name: "geo", title: "Geo helpers", created_by: BEA_ID, created_at: iso(T0 + DAY), updated_at: iso(T0 + 2 * DAY) } }],
  ]);
  const A = { plugin: PLUGIN_ACME, ns: ACME_NS };
  const recs: VersionRec[] = [
    makeVersion({ ...A, id: V.purged, version: "0.0.5", state: "purged", by: BEA_ID, at: T0 - 40 * DAY, extra: { yank_reason: "superseded", purge_after: iso(T0 - 5 * DAY) } }),
    makeVersion({ ...A, id: V.yanked, version: "0.0.7", state: "yanked", by: BEA_ID, at: T0 - 9 * DAY, extra: { yank_reason: "security", yank_note: "Sent data to an outside host. Use 0.1.0.", purge_after: iso(T0 + 21 * DAY) }, usage: { runs_30d: 2, model_versions: ["mv_01jaf3e1f2g3h4j5k6m7n8p9z1"] } }),
    makeVersion({ ...A, id: V.rejected, version: "0.0.8", state: "rejected", by: BEA_ID, at: T0 - 6 * DAY, extra: { purge_after: iso(T0 + 24 * DAY) } }),
    makeVersion({ ...A, id: V.refused, version: "0.0.9", state: "inspect_failed", by: ANA_ID, at: T0 - 3 * DAY, problems: REFUSED_PROBLEMS(ACME_NS), extra: { purge_after: iso(T0 + 27 * DAY) } }),
    makeVersion({ ...A, id: V.approved, version: "0.1.0", state: "approved", by: BEA_ID, at: T0 - 2 * DAY, usage: { runs_30d: 14, model_versions: ["mv_01jaf3e1f2g3h4j5k6m7n8p9q0"], endpoints: ["store-forecast"], dashboards: ["dsh_01jaf3d1a2b3c4d5e6f7g8h9j0"] } }),
    makeVersion({ ...A, id: V.awaiting, version: "0.2.0", state: "awaiting_approval", by: BEA_ID, at: T0, hostile: true }),
    makeVersion({ ...A, id: V.awaitingOwn, version: "0.2.1", state: "awaiting_approval", by: ANA_ID, at: T0 + DAY / 2 }),
    makeVersion({ ...A, id: V.privateOwn, version: "0.3.0", state: "kit_passed", by: ANA_ID, at: T0 + DAY, usage: { runs_30d: 3 } }),
    makeVersion({ ...A, id: V.kitFailed, version: "0.3.1", state: "kit_failed", by: ANA_ID, at: T0 + 1.5 * DAY, kit: FAILING_KIT(ACME_NS), extra: { purge_after: iso(T0 + 31 * DAY) } }),
    makeVersion({ plugin: PLUGIN_GEO, ns: GEO_NS, id: V.uploaded, version: "0.1.0", state: "uploaded", by: BEA_ID, at: T0 + 2 * DAY }),
    makeVersion({ plugin: PLUGIN_GEO, ns: GEO_NS, id: V.inspecting, version: "0.1.1", state: "inspecting", by: BEA_ID, at: T0 + 2 * DAY + 60_000 }),
    makeVersion({ plugin: PLUGIN_GEO, ns: GEO_NS, id: V.describing, version: "0.1.2", state: "describing", by: BEA_ID, at: T0 + 2 * DAY + 120_000 }),
  ];
  const byId = new Map(recs.map((r) => [r.v.id, r]));
  const approved = byId.get(V.approved)!;
  approved.v.approvals = [decision("approve", ANA_ID, approved.v, T0 - DAY)];
  const yanked = byId.get(V.yanked)!;
  yanked.v.approvals = [decision("approve", ANA_ID, yanked.v, T0 - 8 * DAY)];
  const rejected = byId.get(V.rejected)!;
  rejected.v.approvals = [decision("reject", ANA_ID, rejected.v, T0 - 5 * DAY, "The chart sends the data to an outside host. Remove the call and upload again.")];
  return {
    codes: new Map([["acme", ACME_CODE]]),
    plugins,
    versions: byId,
    tickets: new Map(),
    settings: new Map([["acme", { private_use: true }]]),
    uploaders: new Map([
      ["acme", [{ user_id: RUI_ID, display_name: "Rui Runner", email: "rui@acme.example", method: "admin_attested", verified_by: ANA_ID, verified_at: iso(T0 - 10 * DAY) }]],
    ]),
    seq: 100,
  };
}

// ------------------------------------------------------------------------------ the fake pipeline

/** Advance a browser upload's version along the clock (lazy: on every read). */
function advance(rec: VersionRec, ns: string): void {
  const s = rec.script;
  if (!s) return;
  const t = Date.now() - s.startedAt;
  const inspectEnd = s.durationMs * 0.375;
  const v = rec.v;
  if (t < inspectEnd) return;
  if (s.final === "inspect_failed") {
    finish(rec, "inspect_failed", { inspection: inspectionFor(ns, v.version, v.overlay_sha256 ?? sha(v.id), REFUSED_PROBLEMS(ns).slice(0, 3), 12), overlay_sha256: null, purge_after: iso(Date.now() + 30 * DAY) });
    return;
  }
  if (v.state === "inspecting") {
    Object.assign(v, { state: "describing", inspection: inspectionFor(ns, v.version, v.overlay_sha256 ?? sha(v.id), [], 18) });
  }
  if (t < s.durationMs) return;
  const failing = s.final === "kit_failed";
  const exports = exportsFor(ns);
  const checks = failing ? FAILING_KIT(ns) : PASSING_KIT(ns);
  finish(rec, s.final, {
    exports_detail: exports,
    exports: exportCounts(exports),
    describe_digest: sha(`${v.id}-describe`),
    base_image_digest: BASE_IMAGE,
    kit: { ok: !failing, checks },
    urls: ["https://example.org/acme"],
    ...(failing ? { purge_after: iso(Date.now() + 30 * DAY) } : {}),
  });
}

function finish(rec: VersionRec, state: PluginVersionState, patch: Partial<PluginVersion>) {
  Object.assign(rec.v, patch, { state, scope: scopeOf(state) });
  rec.script = null;
}

// ------------------------------------------------------------------------------ callers

interface Caller {
  id: string;
  tenant: string;
  project: string;
  admin: boolean;
  role: string | null;
}

function tenantAdmins(tenant: string): string[] {
  const personas = Object.values(PERSONAS)
    .filter((p) => p.tenants[tenant] === "admin")
    .map((p) => p.id);
  return [...personas, ...(EXTRA_ADMINS[tenant] ?? [])];
}

function caller(request: Request): Caller | Response {
  const p = mockPersona();
  if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");
  const key = request.headers.get("X-Mapi-Project") ?? "";
  const [tenant = "", project = ""] = key.split("/");
  if (!tenant || !project) return problem(422, "SCHEMA_INVALID", "Invalid request", "Name the project in the X-Mapi-Project header.");
  const persona = PERSONAS[Object.keys(PERSONAS).find((k) => PERSONAS[k]?.id === p.id) ?? ""];
  return { id: p.id, tenant, project, admin: persona?.tenants[tenant] === "admin", role: mockProjectRole(key) };
}

/** `require_plugins_available`: turned on, and the tenant is not on the public tier. */
function unavailable(tenant: string): Response | null {
  if (read(MOCK_PLUGINS_OFF_KEY) === "1") return problem(403, "FORBIDDEN", "Not allowed", "Uploaded code is turned off in this deployment.");
  if (tenant === "globex") {
    return problem(403, "FORBIDDEN", "Not allowed", "Public workspaces cannot upload code. Built-in models and declarative specs are always available.");
  }
  return null;
}

const UPLOAD_RANK = new Set(["editor", "runner", "admin"]);

/** `require_can_upload` (D1 + D2): a tenant admin, or an attested editor of the project. */
function cannotUpload(state: PluginsState, c: Caller): Response | null {
  const off = unavailable(c.tenant);
  if (off) return off;
  if (c.admin) return null;
  const attested = (state.uploaders.get(c.tenant) ?? []).some((u) => u.user_id === c.id);
  if (!c.role || !UPLOAD_RANK.has(c.role) || !attested) {
    return problem(403, "FORBIDDEN", "Not allowed", "Only workspace admins and people an admin has confirmed can upload code. Ask a workspace admin.");
  }
  return null;
}

function visible(rec: VersionRec, c: Caller): boolean {
  if (c.admin || rec.v.uploaded_by === c.id) return true;
  if (rec.v.state === "approved") return true;
  return rec.v.state === "yanked" && (rec.v.approvals ?? []).some((a) => a.decision === "approve");
}

function summary(v: PluginVersion): PluginVersionSummary {
  return {
    id: v.id,
    plugin_id: v.plugin_id,
    version: v.version,
    state: v.state,
    scope: v.scope,
    uploaded_by: v.uploaded_by,
    uploaded_at: v.uploaded_at,
    overlay_sha256: v.overlay_sha256 ?? null,
    describe_digest: v.describe_digest ?? null,
    exports: v.exports ?? {},
  };
}

function versionsOf(state: PluginsState, pluginId: string, c: Caller): VersionRec[] {
  const ns = state.plugins.get(pluginId)?.p.namespace ?? "";
  const out = [...state.versions.values()].filter((r) => r.v.plugin_id === pluginId && r.tenant === c.tenant);
  for (const r of out) advance(r, ns);
  return out.filter((r) => visible(r, c)).sort((a, b) => b.v.uploaded_at.localeCompare(a.v.uploaded_at));
}

function pluginDto(state: PluginsState, rec: PluginRec, c: Caller): Plugin {
  const versions = versionsOf(state, rec.p.id, c);
  return { ...rec.p, latest_version: versions[0] ? summary(versions[0].v) : null, versions: versions.length };
}

function pluginFor(state: PluginsState, id: string, c: Caller): PluginRec | Response {
  const rec = state.plugins.get(id);
  if (!rec || rec.tenant !== c.tenant) return problem(404, "NOT_FOUND", "Not found", "no such plugin");
  if (!c.admin && rec.p.created_by !== c.id && versionsOf(state, id, c).length === 0) return problem(404, "NOT_FOUND", "Not found", "no such plugin");
  return rec;
}

function versionFor(state: PluginsState, pluginId: string, versionId: string, c: Caller): VersionRec | Response {
  const rec = state.versions.get(versionId);
  if (!rec || rec.v.plugin_id !== pluginId || rec.tenant !== c.tenant) return problem(404, "NOT_FOUND", "Not found", "no such plugin version");
  advance(rec, state.plugins.get(pluginId)?.p.namespace ?? "");
  if (!visible(rec, c)) return problem(404, "NOT_FOUND", "Not found", "no such plugin version");
  return rec;
}

const isResponse = (v: unknown): v is Response => v instanceof Response;

function conflict(detail: string) {
  return problem(409, "PLUGIN_STATE_CONFLICT", "The version changed", detail);
}

function nextVersionId(state: PluginsState): string {
  state.seq += 1;
  return vid(state.seq);
}

function nextId(prefix: string, state: PluginsState): string {
  state.seq += 1;
  return `${prefix}01jbz3q2x8m9v7k5h4g3f${state.seq.toString(36).padStart(5, "0")}`;
}

function newTenantCode(state: PluginsState): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz234567";
  for (;;) {
    const code = Array.from({ length: 6 }, () => alphabet[Math.floor(Math.random() * 32)] ?? "a").join("");
    if (![...state.codes.values()].includes(code)) return code;
  }
}

// ------------------------------------------------------------------------------ the tenant catalog view

/**
 * The caller's tenant catalog view (D11): the base catalog plus the uploaded entries the
 * caller may use: approved versions, and their own kit-passed or waiting versions while the
 * tenant allows private use. The tenant is the one `X-Mapi-Project` names (when the persona
 * is a member), else the persona's only tenant, else none (the base catalog).
 */
export function withUploadedEntries(base: CatalogResponse, state: PluginsState, request: Request): CatalogResponse {
  const p = mockPersona();
  if (!p || read(MOCK_PLUGINS_OFF_KEY) === "1") return base;
  const persona = Object.values(PERSONAS).find((x) => x.id === p.id);
  if (!persona) return base;
  const members = new Set(
    [...Object.entries(persona.tenants).map(([t]) => t), ...Object.keys(persona.projects).map((k) => k.split("/")[0] ?? "")].filter(
      (t) => mockProjectRole(Object.keys(persona.projects).find((k) => k.startsWith(`${t}/`)) ?? `${t}/-`) !== "client" || persona.tenants[t] != null,
    ),
  );
  const header = request.headers.get("X-Mapi-Project");
  const named = header ? (header.split("/")[0] ?? "") : null;
  const tenant = named !== null ? (members.has(named) ? named : null) : members.size === 1 ? [...members][0] ?? null : null;
  if (!tenant) return base;
  const privateUse = state.settings.get(tenant)?.private_use ?? true;
  const models = [...(base.models ?? [])];
  const steps = [...(base.steps ?? [])];
  const plots = [...(base.plots ?? [])];
  for (const rec of state.versions.values()) {
    if (rec.tenant !== tenant) continue;
    advance(rec, state.plugins.get(rec.v.plugin_id)?.p.namespace ?? "");
    const v = rec.v;
    const usable = v.state === "approved" || ((v.state === "kit_passed" || v.state === "awaiting_approval") && privateUse && v.uploaded_by === p.id);
    if (!usable || !v.exports_detail) continue;
    const provider = `upload:${v.id}`;
    const overlay_hash = v.overlay_sha256 ?? null;
    // Newer versions of the same namespace replace older ones (one entry per id).
    const drop = <T,>(list: T[], id: (x: T) => string, ids: string[]) => {
      for (let i = list.length - 1; i >= 0; i--) if (ids.includes(id(list[i] as T))) list.splice(i, 1);
    };
    drop(models, (m) => m.manifest.id, (v.exports_detail.models ?? []).map((m) => m.manifest.id));
    drop(steps, (s) => s.id, (v.exports_detail.steps ?? []).map((s) => s.id));
    drop(plots, (x) => x.id, (v.exports_detail.plots ?? []).map((x) => x.id));
    for (const m of v.exports_detail.models ?? []) models.push({ ...m, provider, overlay_hash });
    for (const s of v.exports_detail.steps ?? []) steps.push({ ...s, provider, overlay_hash });
    for (const x of v.exports_detail.plots ?? []) plots.push({ ...x, provider, overlay_hash });
  }
  return { ...base, models, steps, plots };
}

// ------------------------------------------------------------------------------ handlers

/**
 * Handlers that must run before the tenancy mock: a sign-in through the mock identity
 * provider also answers a pending reauth (`mapi.mock.pluginReauth`), then falls through.
 */
export function createPluginAuthHandlers(): HttpHandler[] {
  return [
    http.post(`${AUTH}/__mock/auth/login`, () => {
      if (read(MOCK_PLUGIN_REAUTH_KEY) === "1") write(MOCK_PLUGIN_REAUTH_KEY, null);
      return undefined;
    }),
  ];
}

export function createPluginHandlers(state: PluginsState = createPluginsState()): HttpHandler[] {
  const R = (path: string) => new RegExp(`^${API_RE}${path}$`);
  /** The path parameters of `path` (a regexp source after the API base) in the request URL. */
  const match = (request: Request, path: string) => {
    const u = new URL(request.url);
    return new RegExp(`^${API_RE}${path}$`).exec(`${u.origin}${u.pathname}`)?.slice(1).map(decodeURIComponent) ?? [];
  };
  return [
    // ------------------------------------------------------------------ plugins
    http.get(`${API}/v1/plugins`, ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const off = unavailable(c.tenant);
      if (off) return off;
      const url = new URL(request.url);
      const want = url.searchParams.get("state");
      const mine = url.searchParams.get("mine") === "true";
      const items = [...state.plugins.values()]
        .filter((r) => r.tenant === c.tenant)
        .filter((r) => !mine || r.p.created_by === c.id)
        .map((r) => ({ r, versions: versionsOf(state, r.p.id, c) }))
        .filter(({ r, versions }) => c.admin || r.p.created_by === c.id || versions.length > 0)
        .filter(({ versions }) => !want || versions.some((x) => x.v.state === want))
        .map(({ r }) => pluginDto(state, r, c));
      return page(items);
    }),
    http.post(`${API}/v1/plugins`, async ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const refused = cannotUpload(state, c);
      if (refused) return refused;
      const body = (await request.json()) as { short_name?: string; title?: string };
      const short = body.short_name ?? "";
      if (!/^[a-z][a-z0-9]{1,15}$/.test(short)) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "The short name must start with a letter and use 2 to 16 lowercase letters and digits.", {
          errors: [{ loc: ["body", "short_name"], msg: "does not match ^[a-z][a-z0-9]{1,15}$", type: "string_pattern_mismatch" }] as unknown as Problem["errors"],
        });
      }
      const title = (body.title ?? "").trim();
      if (!title || title.length > 120) return problem(422, "SCHEMA_INVALID", "Invalid request", "The title must have 1 to 120 characters.");
      let code = state.codes.get(c.tenant);
      if (!code) {
        code = newTenantCode(state);
        state.codes.set(c.tenant, code);
      }
      const namespace = `t_${code}_${short}`;
      if ([...state.plugins.values()].some((r) => r.p.namespace === namespace)) {
        return problem(409, "PLUGIN_NAMESPACE_TAKEN", "Name taken", `The name ${short} is already used in this workspace. Choose a different name.`);
      }
      const now = iso(Date.now());
      const rec: PluginRec = { tenant: c.tenant, p: { id: nextId("plg_", state), namespace, short_name: short, title, created_by: c.id, created_at: now, updated_at: now } };
      state.plugins.set(rec.p.id, rec);
      return HttpResponse.json(pluginDto(state, rec, c), { status: 201 });
    }),
    http.get(R(`/v1/plugins/${ID}`), ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const off = unavailable(c.tenant);
      if (off) return off;
      const [id = ""] = match(request, `/v1/plugins/${ID}`);
      const rec = pluginFor(state, id, c);
      return isResponse(rec) ? rec : HttpResponse.json(pluginDto(state, rec, c));
    }),

    // ------------------------------------------------------------------ uploads
    http.post(R(`/v1/plugins/${ID}/uploads`), async ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const refused = cannotUpload(state, c);
      if (refused) return refused;
      const [id = ""] = match(request, `/v1/plugins/${ID}/uploads`);
      const plugin = pluginFor(state, id, c);
      if (isResponse(plugin)) return plugin;
      const body = (await request.json()) as { filename?: string; size?: number };
      const filename = body.filename ?? "";
      const size = Number(body.size ?? 0);
      if (!/^[A-Za-z0-9._-]+\.whl$/.test(filename) || filename.length > 255) {
        return problem(422, "PLUGIN_REFUSED", "Upload refused", "Upload a wheel (.whl). `mapi plugin push` builds one for you.", { details: { stage: "upload", checks: ["wheel_tag"] } } as Partial<Problem>);
      }
      if (size > MAX_BYTES) return problem(413, "PAYLOAD_TOO_LARGE", "File too large", "The wheel is larger than 20 MiB.");
      if (size < 1) return problem(422, "SCHEMA_INVALID", "Invalid request", "The file is empty.");
      const today = [...state.tickets.values()].filter((t) => t.by === c.id).length;
      if (today >= 20) return problem(429, "RATE_LIMITED", "Too many uploads", "You can upload 20 files a day. Try again tomorrow.");
      const uploadId = nextId("pup_", state);
      state.tickets.set(uploadId, { tenant: c.tenant, pluginId: id, filename, size, by: c.id, received: null, used: false });
      const expires = iso(Date.now() + 15 * 60_000);
      const key = `t/${c.tenant}/plugins/${uploadId}.whl`;
      return HttpResponse.json(
        {
          upload_id: uploadId,
          expires_at: expires,
          max_bytes: MAX_BYTES,
          post: {
            url: MOCK_PLUGIN_BUCKET,
            method: "POST",
            prefix: key,
            content_length_range: [1, MAX_BYTES],
            create_only: true,
            expires_at: expires,
            fields: { key, policy: btoa(JSON.stringify({ conditions: [] })), "x-goog-signature": "mock" },
          },
        },
        { status: 201 },
      );
    }),
    // The fake quarantine bucket (a POST policy upload: fields, then the file).
    http.post(MOCK_PLUGIN_BUCKET, async ({ request }) => {
      const form = await request.formData();
      const rawKey = form.get("key");
      const key = typeof rawKey === "string" ? rawKey : "";
      const file = form.get("file");
      const uploadId = key.split("/").at(-1)?.replace(/\.whl$/, "") ?? "";
      const ticket = state.tickets.get(uploadId);
      // Duck-typed: jsdom (tests) and the browser have different Blob classes.
      if (!ticket || typeof file !== "object" || file === null || typeof (file as Blob).size !== "number") return new HttpResponse(null, { status: 400 });
      if (ticket.received !== null) return new HttpResponse(null, { status: 412 });
      if ((file as Blob).size < 1 || (file as Blob).size > MAX_BYTES) return new HttpResponse(null, { status: 400 });
      ticket.received = (file as Blob).size;
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(R(`/v1/plugins/${ID}/uploads/${ID}:complete`), async ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const refused = cannotUpload(state, c);
      if (refused) return refused;
      const [id = "", uploadId = ""] = match(request, `/v1/plugins/${ID}/uploads/${ID}:complete`);
      const plugin = pluginFor(state, id, c);
      if (isResponse(plugin)) return plugin;
      const ticket = state.tickets.get(uploadId);
      if (!ticket || ticket.pluginId !== id || ticket.by !== c.id) return problem(404, "NOT_FOUND", "Not found", "no such upload");
      if (ticket.used) return problem(409, "CONFLICT", "Conflict", "This upload was already completed.");
      if (ticket.received === null) return problem(412, "PRECONDITION_FAILED", "File not uploaded", "The file is not in storage yet. Upload it, then try again.");
      if (ticket.received !== ticket.size) {
        return problem(422, "PLUGIN_REFUSED", "Upload refused", `The file has ${ticket.received} bytes, not ${ticket.size}. Upload it again.`, { details: { stage: "upload", checks: ["archive_size"] } } as Partial<Problem>);
      }
      const body = (await request.json()) as { project?: string; request_approval?: boolean };
      const [, version = "0.0.0"] = ticket.filename.split("-");
      if ([...state.versions.values()].some((r) => r.v.plugin_id === id && r.v.version === version)) {
        return problem(409, "PLUGIN_VERSION_EXISTS", "Version exists", `Version ${version} was already uploaded. Change the version number and build the wheel again.`);
      }
      ticket.used = true;
      const ns = plugin.p.namespace;
      const name = ticket.filename.toLowerCase();
      const distOk = name.startsWith(`${ns}-`);
      const final: Script["final"] = !distOk || name.includes("refused") ? "inspect_failed" : name.includes("failing") ? "kit_failed" : body.request_approval ? "awaiting_approval" : "kit_passed";
      const rec = makeVersion({ plugin: id, ns, id: nextVersionId(state), version, state: "inspecting", by: c.id, at: Date.now() });
      rec.tenant = c.tenant;
      rec.v.overlay_sha256 = sha(`${rec.v.id}-archive`);
      rec.script = { startedAt: Date.now(), final, durationMs: pluginMs() };
      state.versions.set(rec.v.id, rec);
      plugin.p.updated_at = iso(Date.now());
      return HttpResponse.json(rec.v, { status: 202 });
    }),

    // ------------------------------------------------------------------ versions
    http.get(R(`/v1/plugins/${ID}/versions`), ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const off = unavailable(c.tenant);
      if (off) return off;
      const [id = ""] = match(request, `/v1/plugins/${ID}/versions`);
      const plugin = pluginFor(state, id, c);
      if (isResponse(plugin)) return plugin;
      return page(versionsOf(state, id, c).map((r) => summary(r.v)));
    }),
    http.get(R(`/v1/plugins/${ID}/versions/${ID}`), ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const off = unavailable(c.tenant);
      if (off) return off;
      const [id = "", versionId = ""] = match(request, `/v1/plugins/${ID}/versions/${ID}`);
      const rec = versionFor(state, id, versionId, c);
      return isResponse(rec) ? rec : HttpResponse.json(rec.v, { headers: { ETag: `"${hex(JSON.stringify(rec.v)).slice(0, 32)}"` } });
    }),
    http.get(R(`/v1/plugins/${ID}/versions/${ID}/usage`), ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const [id = "", versionId = ""] = match(request, `/v1/plugins/${ID}/versions/${ID}/usage`);
      const rec = versionFor(state, id, versionId, c);
      return isResponse(rec) ? rec : HttpResponse.json(rec.usage);
    }),
    http.get(R(`/v1/plugins/${ID}/versions/${ID}/examples/${ID}`), async ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const [id = "", versionId = "", plotId = ""] = match(request, `/v1/plugins/${ID}/versions/${ID}/examples/${ID}`);
      const rec = versionFor(state, id, versionId, c);
      if (isResponse(rec)) return rec;
      const plot = (rec.v.exports_detail?.plots ?? []).find((x) => x.id === plotId);
      if (!plot) return problem(404, "NOT_FOUND", "Not found", "no example for this chart");
      const source = (CATALOG.plots ?? []).find((x) => x.id === EXAMPLE_SOURCE);
      const { plots } = (await import("./plot-examples.gen.json")).default as { plots: Record<string, PlotSpec> };
      const spec = source ? plots[`${source.id}@${source.version}`] : undefined;
      return spec ? HttpResponse.json({ ...spec, id: plot.id, title: plot.title }) : problem(404, "NOT_FOUND", "Not found", "no example for this chart");
    }),
    http.post(R(`/v1/plugins/${ID}/versions/${ID}:request-approval`), ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const off = unavailable(c.tenant);
      if (off) return off;
      const [id = "", versionId = ""] = match(request, `/v1/plugins/${ID}/versions/${ID}:request-approval`);
      const rec = versionFor(state, id, versionId, c);
      if (isResponse(rec)) return rec;
      if (!c.admin && rec.v.uploaded_by !== c.id) return problem(403, "FORBIDDEN", "Not allowed", "Only the uploader or a workspace admin can ask for a review.");
      if (rec.v.state !== "kit_passed") return conflict("Only a version that passed its tests can be sent for review.");
      finish(rec, "awaiting_approval", {});
      return HttpResponse.json(rec.v);
    }),
    ...(["approve", "reject"] as const).map((d) =>
      http.post(R(`/v1/plugins/${ID}/versions/${ID}:${d}`), async ({ request }) => {
        const c = caller(request);
        if (isResponse(c)) return c;
        const off = unavailable(c.tenant);
        if (off) return off;
        const [id = "", versionId = ""] = match(request, `/v1/plugins/${ID}/versions/${ID}:${d}`);
        const rec = versionFor(state, id, versionId, c);
        if (isResponse(rec)) return rec;
        if (!c.admin) return problem(403, "FORBIDDEN", "Not allowed", "Only workspace admins can approve or reject uploaded code.");
        if (read(MOCK_PLUGIN_REAUTH_KEY) === "1") {
          return problem(401, "REAUTH_REQUIRED", "Sign in again", "Approving uploaded code needs a sign-in from the last 15 minutes. Sign in again, then try again.");
        }
        if (rec.v.uploaded_by === c.id) {
          return problem(403, "SELF_APPROVAL_FORBIDDEN", "Not allowed", "the person who submitted a plugin version cannot approve it");
        }
        const body = (await request.json()) as { overlay_sha256?: string; describe_digest?: string; base_image_digest?: string; note?: string | null };
        const note = body.note?.trim() || null;
        if (d === "reject" && !note) return problem(422, "SCHEMA_INVALID", "Invalid request", "A rejection needs a note. Tell the uploader what to change.");
        if (rec.v.state !== "awaiting_approval") return conflict("This version is not waiting for a reviewer any more. Reload the page.");
        if (body.overlay_sha256 !== rec.v.overlay_sha256 || body.describe_digest !== rec.v.describe_digest || body.base_image_digest !== rec.v.base_image_digest) {
          return conflict("The version changed after you opened the page. Reload the page and check it again.");
        }
        rec.v.approvals = [...(rec.v.approvals ?? []), decision(d, c.id, rec.v, Date.now(), note)];
        finish(rec, d === "approve" ? "approved" : "rejected", d === "reject" ? { purge_after: iso(Date.now() + 30 * DAY) } : {});
        return HttpResponse.json(rec.v);
      }),
    ),
    http.post(R(`/v1/plugins/${ID}/versions/${ID}:yank`), async ({ request }) => {
      const c = caller(request);
      if (isResponse(c)) return c;
      const off = unavailable(c.tenant);
      if (off) return off;
      const [id = "", versionId = ""] = match(request, `/v1/plugins/${ID}/versions/${ID}:yank`);
      const rec = versionFor(state, id, versionId, c);
      if (isResponse(rec)) return rec;
      if (!c.admin && rec.v.uploaded_by !== c.id) return problem(403, "FORBIDDEN", "Not allowed", "Only the uploader or a workspace admin can withdraw a version.");
      if (!["kit_passed", "awaiting_approval", "approved"].includes(rec.v.state)) return conflict("Only a version that runs can use can be withdrawn.");
      const body = (await request.json()) as { reason?: string; note?: string | null };
      if (!["withdrawn", "security", "superseded"].includes(body.reason ?? "")) return problem(422, "SCHEMA_INVALID", "Invalid request", "Give the reason: withdrawn, security or superseded.");
      finish(rec, "yanked", { yank_reason: body.reason as PluginVersion["yank_reason"], yank_note: body.note?.trim() || null, purge_after: iso(Date.now() + 30 * DAY) });
      return HttpResponse.json(rec.v);
    }),

    // ------------------------------------------------------------------ tenant settings (admins)
    http.get(R(`/v1/tenants/${ID}/plugin-settings`), ({ request }) => {
      const [tenant = ""] = match(request, `/v1/tenants/${ID}/plugin-settings`);
      const refused = needTenantAdmin(tenant);
      if (refused) return refused;
      return HttpResponse.json(settingsDto(state, tenant));
    }),
    http.patch(R(`/v1/tenants/${ID}/plugin-settings`), async ({ request }) => {
      const [tenant = ""] = match(request, `/v1/tenants/${ID}/plugin-settings`);
      const refused = needTenantAdmin(tenant);
      if (refused) return refused;
      const body = (await request.json()) as { private_use?: unknown };
      if (typeof body.private_use !== "boolean") return problem(422, "SCHEMA_INVALID", "Invalid request", "private_use must be true or false.");
      state.settings.set(tenant, { private_use: body.private_use });
      return HttpResponse.json(settingsDto(state, tenant));
    }),
    http.get(R(`/v1/tenants/${ID}/plugin-uploaders`), ({ request }) => {
      const [tenant = ""] = match(request, `/v1/tenants/${ID}/plugin-uploaders`);
      const refused = needTenantAdmin(tenant);
      if (refused) return refused;
      return HttpResponse.json(state.uploaders.get(tenant) ?? []);
    }),
    http.put(R(`/v1/tenants/${ID}/plugin-uploaders/${ID}`), ({ request }) => {
      const [tenant = "", userId = ""] = match(request, `/v1/tenants/${ID}/plugin-uploaders/${ID}`);
      const refused = needTenantAdmin(tenant);
      if (refused) return refused;
      const me = mockPersona();
      if (me?.id === userId) return problem(403, "FORBIDDEN", "Not allowed", "You can't confirm yourself. Admins can already upload.");
      const persona = Object.values(PERSONAS).find((p) => p.id === userId);
      if (!persona || !(tenant in persona.tenants || Object.keys(persona.projects).some((k) => k.startsWith(`${tenant}/`)))) {
        return problem(404, "NOT_FOUND", "Not found", "no such member");
      }
      const list = state.uploaders.get(tenant) ?? [];
      const existing = list.find((u) => u.user_id === userId);
      if (existing) return HttpResponse.json(existing);
      const row: VerifiedUploader = { user_id: userId, display_name: persona.name, email: persona.email, method: "admin_attested", verified_by: me?.id ?? "", verified_at: iso(Date.now()) };
      state.uploaders.set(tenant, [...list, row]);
      return HttpResponse.json(row);
    }),
    http.delete(R(`/v1/tenants/${ID}/plugin-uploaders/${ID}`), ({ request }) => {
      const [tenant = "", userId = ""] = match(request, `/v1/tenants/${ID}/plugin-uploaders/${ID}`);
      const refused = needTenantAdmin(tenant);
      if (refused) return refused;
      state.uploaders.set(tenant, (state.uploaders.get(tenant) ?? []).filter((u) => u.user_id !== userId));
      return new HttpResponse(null, { status: 204 });
    }),
  ];
}

function needTenantAdmin(tenant: string): Response | null {
  const p = mockPersona();
  if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");
  if (!tenantAdmins(tenant).includes(p.id)) return problem(403, "FORBIDDEN", "Not allowed", "Only workspace admins can change the uploaded-code settings.");
  return null;
}

function settingsDto(state: PluginsState, tenant: string): PluginSettings {
  const off = read(MOCK_PLUGINS_OFF_KEY) === "1";
  const publicTier = tenant === "globex";
  return {
    tenant_code: state.codes.get(tenant) ?? null,
    private_use: state.settings.get(tenant)?.private_use ?? true,
    uploads_allowed: !off && !publicTier,
    reason: off ? "Uploaded code is turned off in this deployment." : publicTier ? "Public workspaces cannot upload code." : "",
  };
}
