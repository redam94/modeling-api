/**
 * The analyst builder's pure model of a dashboard draft (plan §3.6.3): defaults for a new
 * dashboard, the edits the Setup and Inputs tabs make, and the checks that mirror
 * `mapi_spec.dashboards.DashboardSpec` so the form says what is wrong before the server does.
 * The server still validates every save (`PUT …/draft`) and every publish (`:validate`).
 *
 * Also the share-link form (read-only by default, plan §10.3) and the role checks of the
 * builder's actions (publish and approving a refit need the runner role, §10.4).
 */
import { atLeast } from "@/api/access";
import type { Issue, ProjectRole } from "@/api/types";

import {
  DEFAULT_SHARE_DAYS,
  MAX_CHANNELS,
  MAX_PASSCODE_LEN,
  MAX_SHARE_DAYS,
  MAX_TILES,
  MIN_PASSCODE_LEN,
  TAB_KEYS,
  TILE_KEYS,
  type ChannelDef,
  type DashboardShareCreate,
  type DashboardSource,
  type DashboardSpec,
  type KpiDef,
  type ShareInput,
  type TabKey,
  type TileKey,
} from "../types";

/** `mapi_spec._types.NAME_RE`: names of dashboards, channels and KPI variables. */
export const NAME_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/;
/** ISO 4217 currency code. */
export const CURRENCY_RE = /^[A-Z]{3}$/;
export const MAX_TITLE = 120;
export const MAX_CLIENT_NAME = 80;
export const MAX_SUMMARY = 600;
export const MAX_NOTE = 2000;
export const MAX_LABEL = 60;
export const MAX_SHARE_LABEL = 80;
export const MAX_MULT = 5;
export const MAX_EXACT_RUNS = 500;
export const SHARE_EXPIRY_DAYS = [7, 30, 90, 180, 365] as const;

export const TILE_LABEL: Record<TileKey, { label: string; hint: string }> = {
  expected: { label: "Expected result", hint: "What the model expects the KPI to be this period, with its range." },
  vs_expected: { label: "Result against the plan", hint: "How far the actual result is above or below the expected one." },
  return_on_media: { label: "Return on media", hint: "What each unit of media spend brings back." },
  in_range_spend: { label: "Spend within range", hint: "The share of spend in channels that are within their best range." },
  accuracy: { label: "Accuracy", hint: "How close the model was to the numbers the client sent." },
  index: { label: "Index", hint: "The result as an index of the first readout (100)." },
};

export const TAB_LABEL: Record<TabKey, { label: string; hint: string }> = {
  this_period: { label: "This period", hint: "Always shown first: the latest readout." },
  trends: { label: "Trends", hint: "The published readouts over 13, 26 or 52 periods." },
  scenarios: { label: "Plan scenarios", hint: "Sliders to try other spend. Needs what-ifs." },
  update: { label: "Update numbers", hint: "Upload new actuals. Needs uploads." },
  history: { label: "History", hint: "All the published readouts." },
};

export const UNIT_LABEL: Record<KpiDef["unit"], string> = {
  count: "Count (orders, visits)",
  currency: "Money",
  ratio: "Ratio",
  percent: "Percent",
};

// ------------------------------------------------------------------------------ defaults

/** A dashboard name (`NAME_RE`) from its title: "Weekly media readout" → "weekly-media-readout". */
export function nameFromTitle(title: string): string {
  const slug = title
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "")
    .replace(/-{2,}/g, "-")
    .slice(0, 100);
  if (!slug) return "dashboard";
  return /^[a-z_]/.test(slug) ? slug : `d-${slug}`;
}

const SHORT_UPPER = new Set(["tv", "ooh", "dooh", "ctv", "seo", "sem", "crm", "sms", "vod", "bvod"]);

/** A channel's label from its name: "paid_search" → "Paid search", "tv" → "TV". */
export function channelLabel(name: string): string {
  const words = name.replace(/^spend_/, "").split(/[_.\-\s]+/).filter(Boolean);
  if (!words.length) return name.slice(0, MAX_LABEL);
  const text = words
    .map((w, i) => (SHORT_UPPER.has(w.toLowerCase()) ? w.toUpperCase() : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase()))
    .join(" ");
  return text.slice(0, MAX_LABEL);
}

/** Channel rows for `names`, keeping the labels and ranges of channels already in `existing`. */
export function channelsFrom(names: readonly string[], existing: readonly ChannelDef[] = []): ChannelDef[] {
  const known = new Map(existing.map((c) => [c.name, c]));
  return [...new Set(names)].slice(0, MAX_CHANNELS).map((n) => known.get(n) ?? { name: n, label: channelLabel(n), min_mult: 0.5, max_mult: 1.5, step: 0.05 });
}

export interface NewDashboard {
  title: string;
  source: DashboardSource;
  kpi: KpiDef;
  channels: readonly string[];
  /** The model has a response grid (what-ifs on); `false` turns what-ifs off. */
  hasGrid: boolean;
}

/** The first draft of a new dashboard: the spec's defaults, what-ifs only with a grid. */
export function defaultSpec(n: NewDashboard): DashboardSpec {
  return {
    layout_version: 1,
    title: n.title.trim(),
    client_name: null,
    summary: null,
    period_label: "week",
    source: n.source,
    kpi: n.kpi,
    channels: channelsFrom(n.channels),
    tiles: ["expected", "vs_expected", "return_on_media", "in_range_spend"],
    tabs: n.hasGrid ? ["this_period", "trends", "scenarios", "history"] : ["this_period", "trends", "history"],
    whatif: { enabled: n.hasGrid, exact: false },
    actuals: { enabled: false, publish: "review", refit_workflow_id: null, allow_refit_requests: false },
    refresh: { workflow_id: null, follow_endpoint: false },
    limits: { exact_runs_per_day: 20 },
    show_technical: false,
    note: null,
  };
}

// ------------------------------------------------------------------------------ edits

/** Add or remove a tile; at most {@link MAX_TILES}, in {@link TILE_KEYS} order. */
export function toggleTile(tiles: readonly TileKey[], key: TileKey): TileKey[] {
  const on = new Set(tiles);
  if (on.has(key)) on.delete(key);
  else if (on.size < MAX_TILES) on.add(key);
  return TILE_KEYS.filter((k) => on.has(k));
}

/** Add or remove a tab; `this_period` always stays and comes first. */
export function toggleTab(tabs: readonly TabKey[], key: TabKey): TabKey[] {
  if (key === "this_period") return normaliseTabs(tabs);
  const on = new Set(tabs);
  if (on.has(key)) on.delete(key);
  else on.add(key);
  return normaliseTabs([...on]);
}

export function normaliseTabs(tabs: readonly TabKey[]): TabKey[] {
  const on = new Set(tabs);
  return TAB_KEYS.filter((k) => k === "this_period" || on.has(k));
}

/** Turn what-ifs on or off (off removes the scenarios tab and exact results). */
export function setWhatIf(spec: DashboardSpec, enabled: boolean): DashboardSpec {
  const tabs = spec.tabs ?? ["this_period"];
  return {
    ...spec,
    whatif: { enabled, exact: enabled ? (spec.whatif?.exact ?? false) : false },
    tabs: enabled ? normaliseTabs([...tabs, "scenarios"]) : tabs.filter((t) => t !== "scenarios"),
  };
}

/** Turn uploads on or off (off removes the update tab and refit requests). */
export function setActuals(spec: DashboardSpec, enabled: boolean): DashboardSpec {
  const tabs = spec.tabs ?? ["this_period"];
  const a = spec.actuals ?? {};
  return {
    ...spec,
    actuals: { enabled, publish: a.publish ?? "review", refit_workflow_id: a.refit_workflow_id ?? null, allow_refit_requests: enabled ? (a.allow_refit_requests ?? false) : false },
    tabs: enabled ? normaliseTabs([...tabs, "update"]) : tabs.filter((t) => t !== "update"),
  };
}

/** Choose the refit workflow; no workflow also turns refit requests off. */
export function setRefitWorkflow(spec: DashboardSpec, workflowId: string | null): DashboardSpec {
  const a = spec.actuals ?? {};
  return { ...spec, actuals: { ...a, refit_workflow_id: workflowId, allow_refit_requests: workflowId ? (a.allow_refit_requests ?? false) : false } };
}

const blank = (v: string | null | undefined): string | null => (v && v.trim() ? v.trim() : null);

/** The spec as it is saved: empty optional texts become `null`, tabs and tiles in order. */
export function cleanSpec(spec: DashboardSpec): DashboardSpec {
  return {
    ...spec,
    title: spec.title.trim(),
    client_name: blank(spec.client_name),
    summary: blank(spec.summary),
    note: blank(spec.note),
    kpi: { ...spec.kpi, label: spec.kpi.label.trim(), currency: spec.kpi.unit === "currency" ? (blank(spec.kpi.currency)?.toUpperCase() ?? null) : null },
    channels: spec.channels.map((c) => ({ ...c, name: c.name.trim(), label: c.label.trim() })),
    tiles: TILE_KEYS.filter((k) => (spec.tiles ?? []).includes(k)),
    tabs: normaliseTabs(spec.tabs ?? []),
    refresh: { workflow_id: spec.refresh?.workflow_id ?? null, follow_endpoint: spec.source.kind === "endpoint" ? (spec.refresh?.follow_endpoint ?? false) : false },
  };
}

/** Two specs are the same draft (to know whether there is something to save). */
export function sameSpec(a: DashboardSpec, b: DashboardSpec): boolean {
  return JSON.stringify(cleanSpec(a)) === JSON.stringify(cleanSpec(b));
}

// ------------------------------------------------------------------------------ checks

export interface FormIssue {
  /** A JSON pointer into the spec (`/channels/0/label`), as the server's `Issue.pointer`. */
  pointer: string;
  message: string;
}

export interface SpecContext {
  /** The response grid's channels, `null` when the model has no grid, `undefined` when unknown. */
  gridChannels?: readonly string[] | null | undefined;
}

/** What is wrong with the draft, in plain words (empty: the server will accept the shape). */
export function specIssues(spec: DashboardSpec, ctx: SpecContext = {}): FormIssue[] {
  const out: FormIssue[] = [];
  const add = (pointer: string, message: string) => out.push({ pointer, message });

  const title = spec.title.trim();
  if (!title) add("/title", "Type a title.");
  else if (title.length > MAX_TITLE) add("/title", `Use ${MAX_TITLE} characters or fewer for the title.`);
  if ((spec.client_name ?? "").trim().length > MAX_CLIENT_NAME) add("/client_name", `Use ${MAX_CLIENT_NAME} characters or fewer for the client name.`);
  if ((spec.summary ?? "").trim().length > MAX_SUMMARY) add("/summary", `Use ${MAX_SUMMARY} characters or fewer for the summary.`);
  if ((spec.note ?? "").trim().length > MAX_NOTE) add("/note", `Use ${MAX_NOTE} characters or fewer for the note.`);

  const src = spec.source;
  if (src.kind === "endpoint" && !src.alias) add("/source/alias", "Choose an endpoint.");
  if (src.kind === "model_version") {
    if (!src.model_id) add("/source/model_id", "Choose a model.");
    else if (!src.version) add("/source/version", "Choose a model version.");
  }

  const k = spec.kpi;
  if (!k.variable.trim()) add("/kpi/variable", "Type the KPI variable of the model (for example revenue).");
  else if (!NAME_RE.test(k.variable)) add("/kpi/variable", "The KPI variable can have only letters, digits, _, . and -, and must start with a letter or _.");
  if (!k.label.trim()) add("/kpi/label", "Type a label for the KPI.");
  else if (k.label.trim().length > MAX_LABEL) add("/kpi/label", `Use ${MAX_LABEL} characters or fewer for the KPI label.`);
  if (k.unit === "currency" && !CURRENCY_RE.test((k.currency ?? "").trim().toUpperCase())) add("/kpi/currency", "Type a currency code of 3 letters (for example EUR).");
  const decimals = k.decimals ?? 0;
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 4) add("/kpi/decimals", "Use 0 to 4 decimals.");
  if (k.value_per_unit !== null && k.value_per_unit !== undefined && !(k.value_per_unit > 0)) add("/kpi/value_per_unit", "The value of one unit must be more than 0.");

  if (!spec.channels.length) add("/channels", "Add at least one channel.");
  if (spec.channels.length > MAX_CHANNELS) add("/channels", `A dashboard can have at most ${MAX_CHANNELS} channels.`);
  const seen = new Set<string>();
  spec.channels.forEach((c, i) => {
    const p = `/channels/${i}`;
    if (!NAME_RE.test(c.name)) add(`${p}/name`, `The channel name “${c.name}” is not correct. Use letters, digits, _, . and -.`);
    else if (seen.has(c.name)) add(`${p}/name`, `The channel ${c.name} is in the list two times.`);
    seen.add(c.name);
    if (!c.label.trim()) add(`${p}/label`, `Type a label for ${c.name}.`);
    else if (c.label.trim().length > MAX_LABEL) add(`${p}/label`, `Use ${MAX_LABEL} characters or fewer for the label of ${c.name}.`);
    const min = c.min_mult ?? 0.5;
    const max = c.max_mult ?? 1.5;
    const step = c.step ?? 0.05;
    if (!(min >= 0) || !(max <= MAX_MULT) || !(min < 1 && 1 < max)) add(`${p}/min_mult`, `The slider range of ${c.label || c.name} must include the current plan: lowest below 1×, highest above 1× and at most ${MAX_MULT}×.`);
    else if (!(step > 0) || step > max - min) add(`${p}/step`, `The slider step of ${c.label || c.name} must be more than 0 and smaller than its range.`);
  });

  const tiles = spec.tiles ?? [];
  if (tiles.length > MAX_TILES) add("/tiles", `Choose at most ${MAX_TILES} tiles.`);
  if (new Set(tiles).size !== tiles.length) add("/tiles", "Each tile can be on the dashboard one time.");
  const tabs = spec.tabs ?? [];
  if (!tabs.length || tabs[0] !== "this_period") add("/tabs", "The first tab must be This period.");

  const whatif = spec.whatif?.enabled ?? true;
  if (whatif && ctx.gridChannels === null) add("/whatif/enabled", "The model has no response grid. Turn off what-ifs.");
  if (whatif && ctx.gridChannels) {
    const grid = new Set(ctx.gridChannels);
    spec.channels.forEach((c, i) => {
      if (!grid.has(c.name)) add(`/channels/${i}/name`, `The response grid has no channel named ${c.name}.`);
    });
  }
  if (tabs.includes("scenarios") && !whatif) add("/tabs", "Plan scenarios needs what-ifs. Turn on what-ifs or remove the tab.");

  const a = spec.actuals ?? {};
  if (tabs.includes("update") && !a.enabled) add("/tabs", "Update numbers needs uploads. Turn on uploads or remove the tab.");
  if (a.allow_refit_requests && !a.enabled) add("/actuals/allow_refit_requests", "Refit requests need uploads. Turn on uploads first.");
  if (a.allow_refit_requests && !a.refit_workflow_id) add("/actuals/refit_workflow_id", "Choose the workflow that refits the model.");

  if (spec.refresh?.follow_endpoint && src.kind !== "endpoint") add("/refresh/follow_endpoint", "Only a dashboard on an endpoint can follow it.");
  const runs = spec.limits?.exact_runs_per_day ?? 20;
  if (!Number.isInteger(runs) || runs < 0 || runs > MAX_EXACT_RUNS) add("/limits/exact_runs_per_day", `Use a whole number from 0 to ${MAX_EXACT_RUNS}.`);
  return out;
}

/** The first message at `pointer` (or under it, with `deep`). */
export function issueAt(issues: readonly (FormIssue | Issue)[], pointer: string, deep = false): string | undefined {
  return issues.find((i) => i.pointer === pointer || (deep && (i.pointer ?? "").startsWith(`${pointer}/`)))?.message;
}

/** The tab of the builder where an issue is fixed. */
export function tabOfIssue(pointer: string | null | undefined): "setup" | "inputs" {
  return /^\/(whatif|actuals|refresh|limits)(\/|$)/.test(pointer ?? "") ? "inputs" : "setup";
}

// ------------------------------------------------------------------------------ share links

export interface ShareForm {
  label: string;
  days: number;
  /** Empty: no passcode. */
  passcode: string;
  /** New links are read-only (§10.3): every input is off until the analyst turns it on. */
  whatif: boolean;
  actuals: boolean;
}

export const DEFAULT_SHARE_FORM: Readonly<ShareForm> = { label: "", days: DEFAULT_SHARE_DAYS, passcode: "", whatif: false, actuals: false };

export interface ShareFormIssues {
  label?: string;
  days?: string;
  passcode?: string;
}

export function shareFormIssues(f: ShareForm): ShareFormIssues {
  const out: ShareFormIssues = {};
  if (f.label.trim().length > MAX_SHARE_LABEL) out.label = `Use ${MAX_SHARE_LABEL} characters or fewer for the label.`;
  if (!Number.isInteger(f.days) || f.days < 1 || f.days > MAX_SHARE_DAYS) out.days = `A link can last 1 to ${MAX_SHARE_DAYS} days.`;
  if (f.passcode && f.passcode.length < MIN_PASSCODE_LEN) out.passcode = `Use at least ${MIN_PASSCODE_LEN} characters for the passcode.`;
  else if (f.passcode.length > MAX_PASSCODE_LEN) out.passcode = `Use ${MAX_PASSCODE_LEN} characters or fewer for the passcode.`;
  return out;
}

/** The inputs a link may allow: only those the dashboard has. */
export function shareInputsOf(spec: Pick<DashboardSpec, "whatif" | "actuals">): ShareInput[] {
  const out: ShareInput[] = [];
  if (spec.whatif?.enabled ?? true) out.push("whatif");
  if (spec.actuals?.enabled) out.push("actuals");
  return out;
}

/** The request body; inputs the dashboard does not have are never sent. */
export function shareCreateBody(f: ShareForm, available: readonly ShareInput[]): DashboardShareCreate {
  const allowed: ShareInput[] = [];
  if (f.whatif && available.includes("whatif")) allowed.push("whatif");
  if (f.actuals && available.includes("actuals")) allowed.push("actuals");
  const body: DashboardShareCreate = { expires_in_days: f.days, allowed_inputs: allowed };
  if (f.label.trim()) body.label = f.label.trim();
  if (f.passcode) body.passcode = f.passcode;
  return body;
}

export const SHARE_INPUT_WORD: Record<ShareInput, string> = { whatif: "Plan scenarios", actuals: "Uploads" };

/** "Read only" or the inputs a link allows, in words. */
export function shareAccessWords(allowed: readonly ShareInput[] | null | undefined): string {
  return allowed?.length ? `Read, plus ${allowed.map((a) => SHARE_INPUT_WORD[a].toLowerCase()).join(" and ")}` : "Read only";
}

// ------------------------------------------------------------------------------ roles

export interface Permission {
  allowed: boolean;
  /** Why not, in words (for the disabled control's explanation). */
  reason: string | null;
}

const RUNNER_REASON = "You need the runner role in this project. Ask a runner or an admin.";

/** Publishing runs live inputs as the publisher: editor + runner. */
export function publishPermission(role: ProjectRole | null | undefined): Permission {
  if (!atLeast(role, "editor")) return { allowed: false, reason: "Only editors and admins can change dashboards." };
  if (!atLeast(role, "runner")) return { allowed: false, reason: `Publishing makes live inputs run as you and use the project's compute. ${RUNNER_REASON}` };
  return { allowed: true, reason: null };
}

/** Approving a refit starts the refit workflow as the approver: editor + runner (§10.4). */
export function refitApprovePermission(role: ProjectRole | null | undefined): Permission {
  if (!atLeast(role, "editor")) return { allowed: false, reason: "Only editors and admins decide refit requests." };
  if (!atLeast(role, "runner")) return { allowed: false, reason: `Approving starts the refit workflow as you and uses the project's compute. ${RUNNER_REASON} You can reject the request.` };
  return { allowed: true, reason: null };
}

/** A share link with inputs spends compute: runner. A read-only link: editor. */
export function shareInputsPermission(role: ProjectRole | null | undefined): Permission {
  if (!atLeast(role, "runner")) return { allowed: false, reason: `A link with inputs runs as you and uses the project's compute. ${RUNNER_REASON}` };
  return { allowed: true, reason: null };
}
