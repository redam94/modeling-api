/**
 * The read-only dashboard tool surface in the browser (plan I-3, I-5, §3.7): the names,
 * arguments and results of `mapi_spec.dashboard_tools.DASHBOARD_TOOLS`, answered over any
 * {@link DashboardClient}, so the chat and MCP adapters of v0.18 can run the same tools in
 * the portal, the analyst's preview and a share link, for a dashboard of either layout.
 *
 * No tool submits a run or writes: `evaluate_whatif` is the grid evaluation, and the client
 * only reads what its viewer may see. `compare_readouts` (with the KPI panel changes of
 * layout 2), `get_channel`, `get_panel` and `get_accuracy` are computed here from readouts
 * with the server's rules (`services/dashboards/tools.py`), `list_panels` and `list_inputs`
 * from the dashboard view, and `explain_metric` from the static glossary. The media tools
 * (`get_channel`, `evaluate_whatif`) refuse a dashboard without the media planner
 * (`DASHBOARD_INPUT_DISABLED`). `tools.test.ts` checks the names, argument properties and
 * glossary keys against `api/dashboard-tools.gen.json` (drift test).
 */
import {
  GLOSSARY_KEYS_V2,
  isLayout2Dashboard,
  MAX_CHANNEL_HISTORY,
  MAX_PANELS,
  MAX_READOUT_PAGE,
  PANEL_ID_RE,
  type AccuracyDetail,
  type AccuracyReport,
  type BatchReport,
  type ChannelDelta,
  type ChannelHistory,
  type ChannelPoint,
  type ClientDashboard,
  type DashboardClient,
  type GlossaryKeyV2,
  type InputsReport,
  type KpiBlock,
  type KpiValue,
  type MetricExplanation,
  type PanelDelta,
  type PanelList,
  type PanelReport,
  type PredictionReport,
  type Readout,
  type ReadoutComparison,
  type ReadoutHeader,
  type ReadoutKind,
  type ReadoutList,
  type TileDelta,
  type WhatIfResult,
} from "./types";

export const DASHBOARD_TOOL_NAMES = [
  "get_dashboard",
  "get_latest_readout",
  "list_readouts",
  "get_readout",
  "compare_readouts",
  "get_channel",
  "explain_metric",
  "evaluate_whatif",
  "list_panels",
  "get_panel",
  "get_accuracy",
  "list_inputs",
  "get_prediction",
  "get_batch_summary",
] as const;
export type DashboardToolName = (typeof DASHBOARD_TOOL_NAMES)[number];

/** `mapi_spec.dashboards.GLOSSARY`: plain (ASD-STE100) definitions behind "What does this mean?". */
export const GLOSSARY: Readonly<Record<GlossaryKeyV2, string>> = {
  expected: "The result that the model calculates from your media spend and the other inputs.",
  vs_expected: "The difference between the actual result and the result that the model expected. A positive number means that the actual result was better.",
  return_on_media: "The result that each 1 unit of media spend brought. For money results, a value above 1 means that the media paid back.",
  marginal_return: "The result that the next 1 unit of spend on a channel brings. It tells you if more spend on the channel is still useful.",
  contribution: "The part of the result that a channel caused, as the model calculates it.",
  share: "The part of all media contribution that comes from one channel.",
  range: "The likely range of a value. The model is 90% sure that the true value is in this range.",
  zones: "Where the spend on a channel sits on its response curve: more room, within range, past its best or not paying back.",
  headroom: "More room: the next unit of spend still brings a good return. You can spend more on this channel.",
  optimal: "Within range: the spend on this channel is near its best level.",
  over: "Past its best: the next unit of spend brings back less than it costs.",
  no_payback: "Not paying back: this channel brings back less than its spend.",
  in_range_spend: "The part of media spend that is in channels within range or with more room.",
  accuracy: "How near the model's predictions were to the actual numbers that you sent.",
  bias: "The average difference between the prediction and the actual number. A positive number means that the model expected more.",
  band_hit_rate: "The percentage of periods where the actual number was in the likely range.",
  index: "The result compared with the model's expectation, where 100 is the expectation.",
  scenario: "A plan with different spend per channel. The model calculates its result. The model does not change.",
  compared: "We compare your numbers with what the model expected. The model does not change.",
  conditioned: "The model uses your numbers to update its forecast. The model parameters do not change.",
  refit: "The analyst fits the model again with new data. Only the analyst can start a refit.",
  prediction: "The value that the model calculates for the case or the period that you selected.",
  likely_range: "The range where one new result will probably be. The model is 90% sure that the result is in this range.",
  probability: "How probable a result is, from 0% (it will not occur) to 100% (it will occur).",
  share_positive: "The percentage of cases where the probability of the selected result is more than the threshold that the analyst set.",
  accuracy_rate: "The percentage of cases where the most probable class was the actual class.",
  calibration: "A check of the probabilities. When the model gives a probability of 70%, the result must occur in approximately 70% of those cases.",
  typical_error: "The average size of the difference between the prediction and the actual number, in the units of the result.",
  effect: "The difference between the actual result and the result that the model calculates without the change.",
  cumulative_effect: "The total effect of the change over all periods since it started.",
  without_intervention: "The result that the model calculates for the time after the change, as if the change did not occur.",
  chance_of_effect: "How probable it is that the change had a positive effect, as the model calculates it.",
  map: "A map of the values that the model calculates for each place. The legend shows the value of each colour.",
  batch: "A file of many cases. The model calculates a prediction for each case, and you can download the predictions as a CSV file.",
  approximate: "This value is approximate. The model gives one value per case without a range, so the total does not show the uncertainty.",
  expected_count: "The number of cases where the result will probably occur. It is the sum of the probabilities of all the cases.",
};

// ------------------------------------------------------------------------------ arguments

export interface ListReadoutsArgs {
  kind?: ReadoutKind | null;
  before_seq?: number | null;
  limit?: number;
}
export interface ReadoutRef {
  seq: number;
}
export interface CompareArgs {
  a_seq: number;
  b_seq: number;
}
export interface ChannelArgs {
  name: string;
  last_n?: number;
}
export interface ExplainArgs {
  metric: GlossaryKeyV2;
}
export interface WhatIfArgs {
  multipliers?: Record<string, number>;
}
/** `get_accuracy` (the latest published actuals readout without `seq`). */
export interface OptionalReadoutRef {
  seq?: number | null;
}
/** `get_panel` (the latest published readout without `seq`). */
export interface PanelArgs {
  panel_id: string;
  seq?: number | null;
}

/** A tool call the arguments of which do not fit (the server's `SCHEMA_INVALID`). */
export class ToolArgsError extends Error {
  override name = "ToolArgsError";
  readonly code = "SCHEMA_INVALID";
}

/** No published, ready readout to answer from (the server's 404). */
export class ToolNotFoundError extends Error {
  override name = "ToolNotFoundError";
  readonly code = "NOT_FOUND";
}

/**
 * A tool that does not apply now: the media tools on a dashboard without the media planner
 * (`DASHBOARD_INPUT_DISABLED`), or a readout that is still running (`NOT_READY`).
 */
export class ToolRefusedError extends Error {
  override name = "ToolRefusedError";
  constructor(
    message: string,
    readonly code: "DASHBOARD_INPUT_DISABLED" | "NOT_READY",
  ) {
    super(message);
  }
}

function noPlanner(): ToolRefusedError {
  return new ToolRefusedError("this dashboard has no media planner: channels and spend plans are not part of it", "DASHBOARD_INPUT_DISABLED");
}

function optionalSeq(v: unknown, what: string): number | null {
  return v === undefined || v === null ? null : seqArg(v, what);
}

function panelIdArg(v: unknown): string {
  if (typeof v !== "string" || !PANEL_ID_RE.test(v)) throw new ToolArgsError("panel_id must be a panel id (lower case letters, digits and _)");
  return v;
}

function seqArg(v: unknown, what: string): number {
  if (typeof v !== "number" || !Number.isInteger(v) || v < 1) throw new ToolArgsError(`${what} must be a whole number of 1 or more`);
  return v;
}

function clampLimit(v: unknown, dflt: number, max: number, what: string): number {
  if (v === undefined || v === null) return dflt;
  if (typeof v !== "number" || !Number.isInteger(v) || v < 1 || v > max) throw new ToolArgsError(`${what} must be a whole number from 1 to ${max}`);
  return v;
}

// ------------------------------------------------------------------------------ number text (as `readouts.fmt`)

const MINUS = "−";

/** A number for plain text: thousands separated, U+2212 for a minus sign (`readouts.fmt`). */
export function fmtPlain(v: number | null | undefined, decimals = 0): string {
  if (v === null || v === undefined) return "no value";
  const text = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return v < 0 && /[1-9]/.test(text) ? MINUS + text : text;
}

/** `readouts.kpi_noun`: "Orders" → "orders"; keeps acronyms ("GMV"). */
export function kpiNoun(label: string): string {
  return label.slice(1) === label.slice(1).toLowerCase() ? label.toLowerCase() : label;
}

// ------------------------------------------------------------------------------ pure tool results

function header(r: Readout): ReadoutHeader {
  const { snapshot: _snapshot, problem: _problem, ...h } = r;
  return h;
}

function snap(r: Readout) {
  return r.status === "ready" ? (r.snapshot ?? null) : null;
}

function delta(a: number | null | undefined, b: number | null | undefined): number | null {
  return a !== null && a !== undefined && b !== null && b !== undefined ? b - a : null;
}

function kpiBlocks(r: Readout): Map<string, KpiBlock> {
  const out = new Map<string, KpiBlock>();
  for (const b of snap(r)?.panels ?? []) if (b.kind === "kpi") out.set(b.panel_id, b);
  return out;
}

/** The change of each KPI panel of either readout (`b`'s order, then `a`'s; `tools._panel_deltas`). */
export function panelDeltasOf(ra: Readout, rb: Readout): PanelDelta[] {
  const pa = kpiBlocks(ra);
  const pb = kpiBlocks(rb);
  const ids = [...pb.keys(), ...[...pa.keys()].filter((k) => !pb.has(k))];
  return ids.slice(0, MAX_PANELS).map((id) => {
    const a = pa.get(id);
    const b = pb.get(id);
    const va = a?.tile.value ?? null;
    const vb = b?.tile.value ?? null;
    return { panel_id: id, label: (b ?? a)?.title ?? id, a: va, b: vb, delta: delta(va, vb) };
  });
}

/** The dashboard fields `compare_readouts` reads (the subject of layout 2, else the KPI). */
export type CompareDashboard = Pick<ClientDashboard, "kpi" | "channels"> & Partial<Pick<ClientDashboard, "subject" | "layout_version">>;

function compareLead(dash: CompareDashboard, ra: Readout, rb: Readout, tiles: TileDelta[], panels: PanelDelta[]): string {
  const kpi = isLayout2Dashboard(dash) && dash.subject ? dash.subject : dash.kpi;
  const expected = tiles.find((t) => t.key === "expected" && t.delta !== null && t.delta !== undefined);
  if (expected && expected.delta !== null && expected.delta !== undefined) {
    const word = expected.delta > 0 ? "more" : kpi.unit === "count" ? "fewer" : "less";
    return Math.abs(expected.delta) > 0
      ? `Readout ${rb.seq} expects ${fmtPlain(Math.abs(expected.delta), kpi.decimals ?? 0)} ${word} ${kpiNoun(kpi.label)} than readout ${ra.seq}.`
      : `Readouts ${ra.seq} and ${rb.seq} expect the same result.`;
  }
  const first = panels.find((p) => p.delta !== null && p.delta !== undefined && p.b !== null && p.b !== undefined);
  if (first && first.delta !== null && first.delta !== undefined && first.b !== null && first.b !== undefined) {
    const decimals = Number.isInteger(first.b) && Number.isInteger(first.delta) ? 0 : 2;
    if (Math.abs(first.delta) > 0) {
      const way = first.delta > 0 ? "up" : "down";
      return `${first.label} went ${way} by ${fmtPlain(Math.abs(first.delta), decimals)} from readout ${ra.seq} to readout ${rb.seq}.`;
    }
    return `${first.label} is the same in readouts ${ra.seq} and ${rb.seq}.`;
  }
  return `Readout ${rb.seq} compared with readout ${ra.seq}.`;
}

/** `compare_readouts` over two readouts already read (`tools._compare`). */
export function compareReadoutsOf(dash: CompareDashboard, ra: Readout, rb: Readout): ReadoutComparison {
  const sa = snap(ra);
  const sb = snap(rb);
  // layout 2 KPI panels copied into the legacy tiles are compared as panels
  const ta = new Map<string, KpiValue>((sa?.tiles ?? []).filter((t) => t.key !== "panel").map((t) => [t.key, t]));
  const tb = new Map<string, KpiValue>((sb?.tiles ?? []).filter((t) => t.key !== "panel").map((t) => [t.key, t]));
  const keys = [...ta.keys(), ...[...tb.keys()].filter((k) => !ta.has(k))];
  const tiles: TileDelta[] = keys.slice(0, 6).map((k) => {
    const a = ta.get(k);
    const b = tb.get(k);
    return { key: (b ?? a)!.key, label: (b ?? a)!.label, a: a?.value ?? null, b: b?.value ?? null, delta: delta(a?.value, b?.value) };
  });
  const ca = new Map((sa?.channels ?? []).map((c) => [c.name, c]));
  const cb = new Map((sb?.channels ?? []).map((c) => [c.name, c]));
  const channels: ChannelDelta[] = [];
  for (const ch of dash.channels ?? []) {
    const a = ca.get(ch.name);
    const b = cb.get(ch.name);
    if (!a && !b) continue;
    channels.push({
      name: ch.name,
      label: ch.label,
      spend_a: a?.spend ?? null,
      spend_b: b?.spend ?? null,
      contribution_a: a?.contribution ?? null,
      contribution_b: b?.contribution ?? null,
      roi_a: a?.roi ?? null,
      roi_b: b?.roi ?? null,
      zone_a: a?.zone ?? null,
      zone_b: b?.zone ?? null,
    });
  }
  const panel_deltas = panelDeltasOf(ra, rb);
  let lead = compareLead(dash, ra, rb, tiles, panel_deltas);
  if (ra.model_version_id !== rb.model_version_id) lead += " The model changed between the two readouts.";
  return { a: header(ra), b: header(rb), same_model: ra.model_version_id === rb.model_version_id, tiles, channels: channels.slice(0, 40), lead: lead.slice(0, 400), panel_deltas };
}

/** `get_channel` over published readouts given newest first (`tools._channel`). */
export function channelHistoryOf(dash: Pick<ClientDashboard, "channels">, name: string, newestFirst: readonly Readout[], lastN = 13): ChannelHistory {
  const ch = (dash.channels ?? []).find((c) => c.name === name);
  if (!ch) throw new ToolNotFoundError(`the dashboard has no channel named ${name}`);
  const points: ChannelPoint[] = [];
  for (const r of newestFirst) {
    if (r.visibility !== "published") continue;
    const found = snap(r)?.channels?.find((c) => c.name === name);
    if (!found) continue;
    points.push({ seq: r.seq, created_at: r.created_at, model_version_id: r.model_version_id, spend: found.spend ?? null, contribution: found.contribution ?? null, roi: found.roi ?? null, mroi: found.mroi ?? null, zone: found.zone ?? null });
    if (points.length >= lastN) break;
  }
  return { name: ch.name, label: ch.label, points: points.reverse() };
}

export function explainMetric(args: ExplainArgs): MetricExplanation {
  if (!(GLOSSARY_KEYS_V2 as readonly string[]).includes(args.metric)) throw new ToolArgsError(`no explanation for ${String(args.metric)}`);
  return { metric: args.metric, text: GLOSSARY[args.metric] };
}

// ------------------------------------------------------------------------------ tools over a client

export async function getDashboard(c: DashboardClient): Promise<ClientDashboard> {
  return c.dashboard();
}

export async function getLatestReadout(c: DashboardClient): Promise<Readout> {
  const d = await c.dashboard();
  if (d.latest_readout) return d.latest_readout;
  throw new ToolNotFoundError("the dashboard has no readout yet");
}

export async function listReadouts(c: DashboardClient, args: ListReadoutsArgs = {}): Promise<ReadoutList> {
  const limit = clampLimit(args.limit, 20, MAX_READOUT_PAGE, "limit");
  const beforeSeq = args.before_seq === undefined || args.before_seq === null ? undefined : seqArg(args.before_seq, "before_seq");
  return c.readoutPage({ kind: args.kind ?? undefined, beforeSeq, limit });
}

export async function getReadout(c: DashboardClient, args: ReadoutRef): Promise<Readout> {
  return c.readout(seqArg(args.seq, "seq"));
}

export async function compareReadouts(c: DashboardClient, args: CompareArgs): Promise<ReadoutComparison> {
  const [d, a, b] = await Promise.all([c.dashboard(), c.readout(seqArg(args.a_seq, "a_seq")), c.readout(seqArg(args.b_seq, "b_seq"))]);
  return compareReadoutsOf(d, a, b);
}

export async function getChannel(c: DashboardClient, args: ChannelArgs): Promise<ChannelHistory> {
  const lastN = clampLimit(args.last_n, 13, MAX_CHANNEL_HISTORY, "last_n");
  const d = await c.dashboard();
  if (!(d.channels ?? []).length) throw noPlanner();
  if (!(d.channels ?? []).some((ch) => ch.name === args.name)) throw new ToolNotFoundError(`the dashboard has no channel named ${args.name}`);
  // Published, ready readouts newest first; pages back until `lastN` points or 200 rows.
  const rows: Readout[] = [];
  let before: number | undefined;
  let scanned = 0;
  while (rows.length < lastN && scanned < 200) {
    const page = await c.readoutPage({ beforeSeq: before, limit: MAX_READOUT_PAGE });
    const items = page.items ?? [];
    scanned += items.length;
    const wanted = items.filter((h) => h.visibility === "published" && h.status === "ready").slice(0, lastN - rows.length);
    rows.push(...(await Promise.all(wanted.map((h) => c.readout(h.seq)))));
    if (page.next_before_seq === null || page.next_before_seq === undefined || !items.length) break;
    before = page.next_before_seq;
  }
  return channelHistoryOf(d, args.name, rows, lastN);
}

export async function evaluateWhatIf(c: DashboardClient, args: WhatIfArgs): Promise<WhatIfResult> {
  if (!((await c.dashboard()).channels ?? []).length) throw noPlanner();
  return c.evaluate(args.multipliers ?? {});
}

// ------------------------------------------------------------------------------ layout 2 tools

/** `list_panels`: the panels of the published revision in reading order (empty on layout 1). */
export async function listPanels(c: DashboardClient): Promise<PanelList> {
  const d = await c.dashboard();
  return { panels: isLayout2Dashboard(d) ? [...(d.panels_meta ?? [])] : [] };
}

/** The newest published, ready readout of `kind` the client sees (pages back over ≤ 200 rows). */
async function latestPublished(c: DashboardClient, kind: ReadoutKind): Promise<Readout | null> {
  let before: number | undefined;
  let scanned = 0;
  while (scanned < 200) {
    const page = await c.readoutPage({ kind, beforeSeq: before, limit: MAX_READOUT_PAGE });
    const items = page.items ?? [];
    scanned += items.length;
    const found = items.find((h) => h.visibility === "published" && h.status === "ready");
    if (found) return c.readout(found.seq);
    if (page.next_before_seq === null || page.next_before_seq === undefined || !items.length) break;
    before = page.next_before_seq;
  }
  return null;
}

function notReady(r: Readout): ToolRefusedError | null {
  return r.status === "queued" || r.status === "running" || r.status === "materialising"
    ? new ToolRefusedError(`readout ${r.seq} is not ready yet: try again in a moment`, "NOT_READY")
    : null;
}

/** `get_panel` over a readout already read (`tools._panel`). */
export function panelOf(r: Readout, panelId: string): PanelReport {
  const waiting = notReady(r);
  if (waiting) throw waiting;
  const panel = snap(r)?.panels?.find((b) => b.panel_id === panelId);
  if (!panel) throw new ToolNotFoundError(`readout ${r.seq} has no panel named ${panelId}`);
  return { seq: r.seq, panel };
}

export async function getPanel(c: DashboardClient, args: PanelArgs): Promise<PanelReport> {
  const panelId = panelIdArg(args.panel_id);
  const seq = optionalSeq(args.seq, "seq");
  const r = seq !== null ? await c.readout(seq) : (await c.dashboard()).latest_readout;
  if (!r) throw new ToolNotFoundError("the dashboard has no readout yet");
  return panelOf(r, panelId);
}

/** `get_accuracy` over a readout already read: layout 1's accuracy reads as the error metrics (`tools._accuracy`). */
export function accuracyOf(r: Readout | null): AccuracyReport {
  if (!r) return { seq: null, detail: null, history: [] };
  const s = snap(r);
  if (!s) return { seq: r.seq, detail: null, history: [] };
  let detail: AccuracyDetail | null = s.accuracy_detail ?? null;
  if (!detail && s.accuracy) detail = { ...s.accuracy, kind: "error" };
  return { seq: r.seq, detail, history: [...(s.accuracy_history ?? [])] };
}

export async function getAccuracy(c: DashboardClient, args: OptionalReadoutRef = {}): Promise<AccuracyReport> {
  const seq = optionalSeq(args.seq, "seq");
  return accuracyOf(seq !== null ? await c.readout(seq) : await latestPublished(c, "actuals"));
}

/**
 * `list_inputs`: the caller's inputs and forms as the dashboard view gives them. The view has
 * no daily caps, so `exact_runs_per_day` and `batch_runs_per_day` are `null` in the browser
 * (the server tool fills them in).
 */
export async function listInputs(c: DashboardClient): Promise<InputsReport> {
  const d = await c.dashboard();
  return {
    inputs: d.inputs ?? {},
    scenario_form: d.scenario_form ?? null,
    batch_form: d.batch_form ?? null,
    exact_runs_per_day: null,
    batch_runs_per_day: null,
  };
}

/**
 * The caller's readout `seq` of `kind` (`tools._own`). The read routes give a client user or a
 * share link only its own private rows; a readout of another kind is "not found".
 */
async function own(c: DashboardClient, seq: number, kind: ReadoutKind): Promise<Readout> {
  const r = await c.readout(seq);
  if (r.kind !== kind) throw new ToolNotFoundError(`readout ${seq} is not a ${kind} readout`);
  return r;
}

export async function getPrediction(c: DashboardClient, args: ReadoutRef): Promise<PredictionReport> {
  const r = await own(c, seqArg(args.seq, "seq"), "prediction");
  return { readout: header(r), prediction: snap(r)?.prediction ?? null };
}

/** `get_batch_summary`: the summary only (never the CSV or its link). */
export async function getBatchSummary(c: DashboardClient, args: ReadoutRef): Promise<BatchReport> {
  const r = await own(c, seqArg(args.seq, "seq"), "batch");
  return { readout: header(r), summary: snap(r)?.batch ?? null };
}

export type ToolResult =
  | ClientDashboard
  | Readout
  | ReadoutList
  | ReadoutComparison
  | ChannelHistory
  | MetricExplanation
  | WhatIfResult
  | PanelList
  | PanelReport
  | AccuracyReport
  | InputsReport
  | PredictionReport
  | BatchReport;

export function isToolName(name: string): name is DashboardToolName {
  return (DASHBOARD_TOOL_NAMES as readonly string[]).includes(name);
}

/** Run one tool by name (the `call_tool` of the server); unknown names are {@link ToolArgsError}. */
export async function callTool(c: DashboardClient, name: string, args: Record<string, unknown> = {}): Promise<ToolResult> {
  if (!isToolName(name)) throw new ToolArgsError(`no dashboard tool named ${JSON.stringify(name)}`);
  switch (name) {
    case "get_dashboard":
      return getDashboard(c);
    case "get_latest_readout":
      return getLatestReadout(c);
    case "list_readouts":
      return listReadouts(c, args);
    case "get_readout":
      return getReadout(c, args as unknown as ReadoutRef);
    case "compare_readouts":
      return compareReadouts(c, args as unknown as CompareArgs);
    case "get_channel":
      if (typeof args.name !== "string") throw new ToolArgsError("name must be a channel name");
      return getChannel(c, args as unknown as ChannelArgs);
    case "explain_metric":
      return explainMetric(args as unknown as ExplainArgs);
    case "evaluate_whatif":
      return evaluateWhatIf(c, args);
    case "list_panels":
      return listPanels(c);
    case "get_panel":
      return getPanel(c, args as unknown as PanelArgs);
    case "get_accuracy":
      return getAccuracy(c, args);
    case "list_inputs":
      return listInputs(c);
    case "get_prediction":
      return getPrediction(c, args as unknown as ReadoutRef);
    case "get_batch_summary":
      return getBatchSummary(c, args as unknown as ReadoutRef);
  }
}
