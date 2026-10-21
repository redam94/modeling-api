/**
 * The read-only dashboard tool surface in the browser (plan I-3, §3.7): the names, arguments
 * and results of `mapi_spec.dashboard_tools.DASHBOARD_TOOLS`, answered over any
 * {@link DashboardClient}, so v0.17's chat can run the same tools in the portal, the analyst's
 * preview and a share link.
 *
 * No tool submits a run or writes: `evaluate_whatif` is the grid evaluation, and the client
 * only reads what its viewer may see. `compare_readouts` and `get_channel` are computed here
 * from readouts with the server's rules (`services/dashboards/tools.py`), and
 * `explain_metric` from the static glossary. `tools.test.ts` checks the names, argument
 * properties and glossary keys against `api/dashboard-tools.gen.json` (drift test).
 */
import {
  GLOSSARY_KEYS,
  MAX_CHANNEL_HISTORY,
  MAX_READOUT_PAGE,
  type ChannelDelta,
  type ChannelHistory,
  type ChannelPoint,
  type ClientDashboard,
  type DashboardClient,
  type GlossaryKey,
  type KpiValue,
  type MetricExplanation,
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
] as const;
export type DashboardToolName = (typeof DASHBOARD_TOOL_NAMES)[number];

/** `mapi_spec.dashboards.GLOSSARY`: plain (ASD-STE100) definitions behind "What does this mean?". */
export const GLOSSARY: Readonly<Record<GlossaryKey, string>> = {
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
  metric: GlossaryKey;
}
export interface WhatIfArgs {
  multipliers?: Record<string, number>;
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

/** `compare_readouts` over two readouts already read (`tools._compare`). */
export function compareReadoutsOf(dash: Pick<ClientDashboard, "kpi" | "channels">, ra: Readout, rb: Readout): ReadoutComparison {
  const sa = snap(ra);
  const sb = snap(rb);
  const ta = new Map<string, KpiValue>((sa?.tiles ?? []).map((t) => [t.key, t]));
  const tb = new Map<string, KpiValue>((sb?.tiles ?? []).map((t) => [t.key, t]));
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
  const expected = tiles.find((t) => t.key === "expected" && t.delta !== null && t.delta !== undefined);
  const noun = kpiNoun(dash.kpi.label);
  let lead: string;
  if (expected && expected.delta !== null && expected.delta !== undefined) {
    const word = expected.delta > 0 ? "more" : dash.kpi.unit === "count" ? "fewer" : "less";
    lead =
      Math.abs(expected.delta) > 0
        ? `Readout ${rb.seq} expects ${fmtPlain(Math.abs(expected.delta), dash.kpi.decimals ?? 0)} ${word} ${noun} than readout ${ra.seq}.`
        : `Readouts ${ra.seq} and ${rb.seq} expect the same result.`;
  } else {
    lead = `Readout ${rb.seq} compared with readout ${ra.seq}.`;
  }
  if (ra.model_version_id !== rb.model_version_id) lead += " The model changed between the two readouts.";
  return { a: header(ra), b: header(rb), same_model: ra.model_version_id === rb.model_version_id, tiles, channels: channels.slice(0, 40), lead: lead.slice(0, 400) };
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
  if (!(GLOSSARY_KEYS as readonly string[]).includes(args.metric)) throw new ToolArgsError(`no explanation for ${String(args.metric)}`);
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
  return c.evaluate(args.multipliers ?? {});
}

export type ToolResult = ClientDashboard | Readout | ReadoutList | ReadoutComparison | ChannelHistory | MetricExplanation | WhatIfResult;

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
  }
}
