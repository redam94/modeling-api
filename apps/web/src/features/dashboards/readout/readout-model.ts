/**
 * The pure model of the readout page: what each tile, scorecard row, history row and check says,
 * in plain words (docs/project/writing-style.md: short sentences, no statistics words on client
 * pages). The page components only lay these out, so the words are tested here once.
 *
 * Nothing here reads the network or the DOM. The what-if numbers come from `whatif.ts` (the port
 * of `mapi_spec.whatif.evaluate_scenario`), so the sliders and the server agree.
 */
import { dmy, fixed, grouped, MINUS, MISSING, signed } from "@/components/ods/format";
import { ZONE_WORDS, type TagTone } from "@/components/ods/tag-word";
import type { DivergingRow, IndexPoint, VersionMark } from "@/components/readout-charts";
import { evaluateScenario, type ResponseGrid } from "@/features/guided/whatif";
import type { Cell } from "@/lib/actuals";

import type {
  ActualsAccuracy,
  ActualsTemplate,
  ChannelDef,
  ChannelReadout,
  ClientDashboard,
  DeltaDir,
  KpiDef,
  KpiValue,
  PeriodLabel,
  Readout,
  ReadoutHeader,
  ReadoutSnapshot,
  RefitStatus,
  Series,
  TabKey,
  Zone,
} from "../types";

type Num = number | null | undefined;
const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);

// ------------------------------------------------------------------------------ periods and tabs

export function periodOf(d: Pick<ClientDashboard, "period_label">): PeriodLabel {
  return d.period_label ?? "week";
}

export function plural(word: string, n: number): string {
  return n === 1 ? word : `${word}s`;
}

const TAB_LABEL: Record<Exclude<TabKey, "this_period">, string> = {
  trends: "Trends",
  scenarios: "Plan scenarios",
  update: "Update numbers",
  history: "History",
};

/** The tab's name: "This week", "Trends", "Plan scenarios", "Update numbers", "History". */
export function tabLabel(tab: TabKey, period: PeriodLabel): string {
  return tab === "this_period" ? `This ${period}` : TAB_LABEL[tab];
}

/** The tabs to show, `this_period` always first, without duplicates. */
export function pageTabs(d: Pick<ClientDashboard, "tabs">): TabKey[] {
  const rest = (d.tabs ?? []).filter((t) => t !== "this_period");
  return ["this_period", ...new Set(rest)];
}

// ------------------------------------------------------------------------------ dates

/** "09.40": the ODS time form (a full stop, 24 hours). */
export function hm(iso: string, timeZone?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", ...(timeZone ? { timeZone } : {}) }).formatToParts(d);
  const h = parts.find((p) => p.type === "hour")?.value ?? "00";
  const m = parts.find((p) => p.type === "minute")?.value ?? "00";
  return `${h}.${m}`;
}

/** DD/MM/YYYY of a timestamp in the viewer's time zone (or `timeZone`). */
export function dateOf(iso: string, timeZone?: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return dmy(iso);
  return new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", ...(timeZone ? { timeZone } : {}) }).format(d);
}

/** "20/10/2026 09.40". */
export function stamp(iso: string, timeZone?: string): string {
  return `${dateOf(iso, timeZone)} ${hm(iso, timeZone)}`;
}

/** The meta line under the title: "Updated 20/10/2026 09.40 with results to the week ending 18/10/2026." */
export function metaLine(r: Pick<ReadoutHeader, "ready_at" | "created_at" | "period"> | null, period: PeriodLabel, timeZone?: string): string {
  if (!r) return "There are no results yet. Your analyst publishes the first readout soon.";
  const at = stamp(r.ready_at ?? r.created_at, timeZone);
  const end = r.period?.[1];
  if (!end) return `Updated ${at}.`;
  const to = period === "period" ? `the period ending ${dmy(end)}` : `the ${period} ending ${dmy(end)}`;
  return `Updated ${at} with results to ${to}.`;
}

// ------------------------------------------------------------------------------ numbers

/** A value in the KPI's unit: "€412,300", "4,210", "1.84", "12.5%". */
export function formatValue(v: Num, unit: KpiValue["unit"], kpi?: Pick<KpiDef, "currency" | "decimals">): string {
  if (!isNum(v)) return MISSING;
  const decimals = kpi?.decimals ?? 0;
  switch (unit) {
    case "currency": {
      const code = kpi?.currency;
      if (!code) return grouped(v, decimals);
      try {
        const text = new Intl.NumberFormat("en-GB", { style: "currency", currency: code, minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(Math.abs(v));
        return v < 0 && Number(v.toFixed(decimals)) !== 0 ? `${MINUS}${text}` : text;
      } catch {
        return `${grouped(v, decimals)} ${code}`;
      }
    }
    case "count":
      return grouped(v, decimals);
    case "ratio":
      return fixed(v, 2);
    case "percent":
      return `${fixed(v, 1)}%`;
    case "index":
      return fixed(v, 0);
    case "probability":
      return `${fixed(v * 100, 1)}%`;
    case "rate":
      return fixed(v, 2);
    case "number":
    case "label":
      return grouped(v, decimals);
  }
}

/** Thousands separated with an explicit sign: "+1,200", "−300", "0". */
export function signedGrouped(v: number, decimals = 0): string {
  const s = grouped(v, decimals);
  return Number(v.toFixed(decimals)) > 0 ? `+${s}` : s;
}

/** The KPI amount without the unit word ("412,300"): for chart axes and table cells. */
export function kpiNumber(kpi: Pick<KpiDef, "decimals">): (v: number) => string {
  return (v: number) => grouped(v, kpi.decimals ?? 0);
}

/** The value of a tile: a signed percentage for "Compared with the model", else per unit. */
export function tileValueText(t: KpiValue, kpi: KpiDef): string {
  if (t.key === "vs_expected" && t.unit === "percent") return isNum(t.value) ? `${signed(t.value, 1)}%` : MISSING;
  if (t.key === "expected" && t.unit === kpi.unit) return formatValue(t.value, t.unit, kpi);
  return formatValue(t.value, t.unit, t.unit === "currency" ? kpi : undefined);
}

/**
 * The change in plain words: "3.2% more than the week before", "About the same as the plan".
 * `delta` is a fraction (0.032). `null` when the tile has no change to report.
 */
export function deltaWords(t: Pick<KpiValue, "delta" | "delta_basis" | "delta_dir">): string | null {
  if (!isNum(t.delta) || !t.delta_basis) return null;
  const pct = Math.abs(t.delta) * 100;
  if (pct < 0.5) return `About the same as ${t.delta_basis}`;
  const words = `${fixed(pct, pct < 10 ? 1 : 0)}% ${t.delta > 0 ? "more" : "less"} than ${t.delta_basis}`;
  return t.delta_dir === "alert" ? `${words}, outside the likely range` : words;
}

export interface TileView {
  key: KpiValue["key"];
  label: string;
  value: string;
  delta: string | null;
  deltaDir: DeltaDir;
  caption: string;
}

/** The tiles of a snapshot in the dashboard's order (at most 4). */
export function tileViews(snapshot: Pick<ReadoutSnapshot, "tiles"> | null | undefined, kpi: KpiDef, order: readonly KpiValue["key"][] = []): TileView[] {
  const tiles = snapshot?.tiles ?? [];
  const byKey = new Map(tiles.map((t) => [t.key, t]));
  const keys = [...new Set([...order, ...tiles.map((t) => t.key)])];
  return keys
    .map((k) => byKey.get(k))
    .filter((t): t is KpiValue => Boolean(t))
    .slice(0, 4)
    .map((t) => ({
      key: t.key,
      label: t.label,
      value: tileValueText(t, kpi),
      delta: deltaWords(t),
      deltaDir: t.delta_dir ?? "flat",
      caption: t.caption ?? "",
    }));
}

/** The return on media that pays back exactly (currency KPI: 1; count KPI: 1 / value per unit). */
export function breakevenOf(kpi: Pick<KpiDef, "unit" | "value_per_unit">): number | null {
  if (kpi.unit === "currency") return 1;
  if (kpi.unit === "count" && isNum(kpi.value_per_unit) && kpi.value_per_unit > 0) return 1 / kpi.value_per_unit;
  return null;
}

/** `mapi_spec.dashboards.zone_of`. */
export function zoneOf(roi: Num, mroi: Num, breakeven: number, band = 0.2): Zone | null {
  if (isNum(roi) && roi < breakeven) return "no_payback";
  if (!isNum(roi) || !isNum(mroi)) return null;
  if (mroi < breakeven) return "over";
  if (mroi > breakeven * (1 + band)) return "headroom";
  return "optimal";
}

export function zoneWord(z: Zone | null | undefined): string {
  return z ? ZONE_WORDS[z] : "No zone";
}

// ------------------------------------------------------------------------------ the channel scorecard

export interface ChannelRow {
  name: string;
  label: string;
  /** The channel's series colour (1-8), by its place in the dashboard's channel list. */
  color: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;
  zone: Zone | null;
  zoneWord: string;
  roi: number | null;
  roiLow: number | null;
  roiHigh: number | null;
  roiText: string;
  contributionText: string;
  shareText: string;
  spendText: string;
}

export function seriesColor(i: number): ChannelRow["color"] {
  return ((((i % 8) + 8) % 8) + 1) as ChannelRow["color"];
}

/** One row per dashboard channel that the snapshot reports (dashboard order; unknown ones last). */
export function channelRows(snapshot: Pick<ReadoutSnapshot, "channels"> | null | undefined, d: Pick<ClientDashboard, "kpi" | "channels">): ChannelRow[] {
  const got = snapshot?.channels ?? [];
  const order = new Map((d.channels ?? []).map((c, i) => [c.name, i]));
  const rows = [...got].sort((a, b) => (order.get(a.name) ?? 1e6) - (order.get(b.name) ?? 1e6));
  const kpi = d.kpi;
  return rows
    .filter((c) => isNum(c.spend) || isNum(c.contribution) || isNum(c.roi))
    .map((c: ChannelReadout) => ({
      name: c.name,
      label: c.label,
      color: seriesColor(order.get(c.name) ?? rows.indexOf(c)),
      zone: c.zone ?? null,
      zoneWord: zoneWord(c.zone),
      roi: isNum(c.roi) ? c.roi : null,
      roiLow: isNum(c.roi_low) ? c.roi_low : null,
      roiHigh: isNum(c.roi_high) ? c.roi_high : null,
      roiText: isNum(c.roi) ? (isNum(c.roi_low) && isNum(c.roi_high) ? `${fixed(c.roi, 2)} (${fixed(c.roi_low, 2)} to ${fixed(c.roi_high, 2)})` : fixed(c.roi, 2)) : MISSING,
      contributionText: formatValue(c.contribution, kpi.unit === "currency" ? "currency" : "count", kpi),
      shareText: isNum(c.share) ? `${fixed(c.share * 100, 0)}%` : MISSING,
      spendText: formatValue(c.spend, "currency", { currency: kpi.unit === "currency" ? (kpi.currency ?? null) : null, decimals: 0 }),
    }));
}

/** The scorecard's columns; the same list labels each value when the grid reflows into cards. */
export const SCORECARD_COLUMNS = [
  { key: "channel", label: "Channel" },
  { key: "zone", label: "Where the spend sits" },
  { key: "roi", label: "Return on media" },
  { key: "contribution", label: "Expected result" },
  { key: "share", label: "Share" },
] as const;
export type ScorecardColumn = (typeof SCORECARD_COLUMNS)[number]["key"];

/** Below this width (px) the scorecard is one card a channel (labels beside values). */
export const SCORECARD_REFLOW_PX = 1099;

/** The text cells of a row with their column labels (the card layout and the CSV use them). */
export function scorecardCells(row: ChannelRow): { key: ScorecardColumn; label: string; text: string }[] {
  const text: Record<ScorecardColumn, string> = {
    channel: row.label,
    zone: row.zoneWord,
    roi: row.roiText,
    contribution: row.contributionText,
    share: row.shareText,
  };
  return SCORECARD_COLUMNS.map((c) => ({ key: c.key, label: c.label, text: text[c.key] }));
}

/** The zones that occur in the rows, in legend order. */
export function zonesIn(rows: readonly ChannelRow[]): Zone[] {
  const seen = new Set(rows.map((r) => r.zone).filter((z): z is Zone => z !== null));
  return (["headroom", "optimal", "over", "no_payback"] as const).filter((z) => seen.has(z));
}

// ------------------------------------------------------------------------------ the KPI series and accuracy

/** The KPI series of a snapshot (named `kpi`, else the first). */
export function kpiSeries(snapshot: Pick<ReadoutSnapshot, "series"> | null | undefined): Series | null {
  const all = snapshot?.series ?? [];
  return all.find((s) => s.name === "kpi") ?? all[0] ?? null;
}

/** The snapshot's accuracy, else the one of its series (periods with an actual number). */
export function accuracyOf(snapshot: Pick<ReadoutSnapshot, "accuracy" | "series"> | null | undefined): ActualsAccuracy | null {
  if (snapshot?.accuracy) return snapshot.accuracy;
  const pts = (kpiSeries(snapshot)?.points ?? []).filter((p) => isNum(p.mean) && isNum(p.actual));
  if (!pts.length) return null;
  let ape = 0;
  let apeN = 0;
  let bias = 0;
  let hits = 0;
  let bandN = 0;
  for (const p of pts) {
    const m = p.mean as number;
    const a = p.actual as number;
    bias += m - a;
    if (a !== 0) {
      ape += Math.abs((m - a) / a);
      apeN += 1;
    }
    if (isNum(p.low) && isNum(p.high)) {
      bandN += 1;
      if (p.low <= a && a <= p.high) hits += 1;
    }
  }
  return { n: pts.length, mape: apeN ? (100 * ape) / apeN : null, bias: bias / pts.length, band_hit_rate: bandN ? (100 * hits) / bandN : null };
}

/** The accuracy card in sentences ("The actual number was in the likely range in 9 of 12 weeks."). */
export function accuracyLines(acc: ActualsAccuracy | null, kpi: KpiDef, period: PeriodLabel): string[] {
  if (!acc || !acc.n) return [];
  const n = acc.n;
  const out: string[] = [];
  if (isNum(acc.band_hit_rate)) {
    const hits = Math.round((acc.band_hit_rate / 100) * n);
    out.push(`The actual number was in the likely range in ${hits} of ${n} ${plural(period, n)}.`);
  }
  if (isNum(acc.mape)) out.push(`On average, the model was ${fixed(acc.mape, 1)}% away from the actual number.`);
  if (isNum(acc.bias) && Math.abs(acc.bias) > 0) {
    const amount = formatValue(Math.abs(acc.bias), kpi.unit === "currency" ? "currency" : "count", kpi);
    out.push(acc.bias > 0 ? `The model expected ${amount} more than the actual number each ${period}, on average.` : `The model expected ${amount} less than the actual number each ${period}, on average.`);
  }
  return out;
}

// ------------------------------------------------------------------------------ what changed since the last readout

/** Channel contribution changes from `prev` to `cur` ("What changed since the week before"). */
export function changeRows(cur: Pick<ReadoutSnapshot, "channels"> | null | undefined, prev: Pick<ReadoutSnapshot, "channels"> | null | undefined): { rows: DivergingRow[]; net: { label: string; value: number } } | null {
  if (!cur || !prev) return null;
  const before = new Map((prev.channels ?? []).map((c) => [c.name, c]));
  const rows: DivergingRow[] = [];
  for (const c of cur.channels ?? []) {
    const p = before.get(c.name);
    if (!p || !isNum(c.contribution) || !isNum(p.contribution)) continue;
    rows.push({ key: c.name, label: c.label, value: c.contribution - p.contribution });
  }
  if (!rows.length) return null;
  return { rows, net: { label: "All media", value: rows.reduce((a, r) => a + r.value, 0) } };
}

// ------------------------------------------------------------------------------ history words

export const KIND_WORD: Record<Readout["kind"], string> = {
  baseline: "First readout",
  refresh: "Regular update",
  scenario: "Saved plan",
  actuals: "Your numbers",
  prediction: "Your prediction",
  batch: "Batch scores",
};

export const STATUS_WORD: Record<Readout["status"], string> = {
  queued: "Waiting to start",
  running: "Calculating",
  materialising: "Almost ready",
  ready: "Ready",
  failed: "Did not finish",
};

export interface Tag {
  label: string;
  tone: TagTone;
}

/** Who can see a readout, when it is not in the shared history. */
export function visibilityTag(v: Readout["visibility"]): Tag | null {
  if (v === "pending_review") return { label: "Waiting for review", tone: "warning" };
  if (v === "private") return { label: "Only you", tone: "neutral" };
  if (v === "hidden") return { label: "Hidden", tone: "neutral" };
  return null;
}

export const REFIT_TAG: Record<RefitStatus, Tag & { text: string }> = {
  open: { label: "Refit requested", tone: "info", text: "You asked the analyst to refit the model with these numbers. The analyst decides. You do not have to do more." },
  approved: { label: "Refit approved", tone: "positive", text: "The analyst approved the refit. A new readout appears in History when the model is ready." },
  rejected: { label: "Refit not done", tone: "neutral", text: "The analyst did not refit the model with these numbers. The comparison stays available." },
  superseded: { label: "Refit replaced", tone: "neutral", text: "A newer refit request replaced this one." },
  failed: { label: "Refit failed", tone: "negative", text: "The refit did not finish. The analyst knows about it." },
};

export function refitTag(s: ReadoutHeader["refit_status"]): (Tag & { text: string }) | null {
  return s ? REFIT_TAG[s] : null;
}

export interface HistoryRow {
  seq: number;
  title: string;
  when: string;
  kind: string;
  status: string;
  ready: boolean;
  failed: boolean;
  tags: Tag[];
}

export function historyRow(h: ReadoutHeader, timeZone?: string): HistoryRow {
  const tags: Tag[] = [];
  const vis = visibilityTag(h.visibility);
  if (vis) tags.push(vis);
  const refit = refitTag(h.refit_status);
  if (refit) tags.push(refit);
  if (h.status === "failed") tags.push({ label: STATUS_WORD.failed, tone: "negative" });
  else if (h.status !== "ready") tags.push({ label: STATUS_WORD[h.status], tone: "info" });
  return {
    seq: h.seq,
    title: h.title,
    when: stamp(h.ready_at ?? h.created_at, timeZone),
    kind: KIND_WORD[h.kind],
    status: STATUS_WORD[h.status],
    ready: h.status === "ready",
    failed: h.status === "failed",
    tags,
  };
}

/** The readouts that form the shared history (published, ready, not saved plans). */
export function isHistoryReadout(h: Pick<ReadoutHeader, "visibility" | "status" | "kind">): boolean {
  return h.visibility === "published" && h.status === "ready" && h.kind !== "scenario";
}

// ------------------------------------------------------------------------------ trends

export interface TrendPoint {
  readout: Pick<Readout, "seq" | "period" | "created_at" | "model_version_id" | "model_ref">;
  snapshot: ReadoutSnapshot | null | undefined;
}

/** The result against the model per readout, as an index (1.00 = as expected), oldest first. */
export function trendIndex(points: readonly TrendPoint[], timeZone?: string): { points: IndexPoint[]; versions: VersionMark[] } {
  const out: IndexPoint[] = [];
  const versions: VersionMark[] = [];
  let model: string | null = null;
  points.forEach((p, i) => {
    const tiles = p.snapshot?.tiles ?? [];
    const vs = tiles.find((t) => t.key === "vs_expected");
    const idx = tiles.find((t) => t.key === "index");
    let value: number | null = null;
    if (vs && isNum(vs.value)) value = 1 + vs.value / 100;
    else if (idx && isNum(idx.value)) value = idx.value / 100;
    const last = (kpiSeries(p.snapshot)?.points ?? []).findLast((q) => isNum(q.mean) && q.mean !== 0);
    const low = last && isNum(last.low) && isNum(last.mean) ? last.low / last.mean : null;
    const high = last && isNum(last.high) && isNum(last.mean) ? last.high / last.mean : null;
    out.push({ t: p.readout.period?.[1] ?? p.readout.created_at.slice(0, 10), value, low, high });
    if (model !== null && p.readout.model_version_id !== model) versions.push({ at: i, label: `Model updated on ${dateOf(p.readout.created_at, timeZone)}` });
    model = p.readout.model_version_id;
  });
  return { points: out, versions };
}

/** One channel's return on media per readout, oldest first. */
export function trendRoi(points: readonly TrendPoint[], channel: string): (number | null)[] {
  return points.map((p) => {
    const c = (p.snapshot?.channels ?? []).find((x) => x.name === channel);
    return c && isNum(c.roi) ? c.roi : null;
  });
}

// ------------------------------------------------------------------------------ scenarios (what-if sliders)

/** The slider bounds of a channel: the analyst's range, inside the grid. */
export function sliderBounds(ch: Pick<ChannelDef, "min_mult" | "max_mult" | "step">, grid: Pick<ResponseGrid, "multipliers">): { min: number; max: number; step: number } {
  const gMin = grid.multipliers[0] ?? 1;
  const gMax = grid.multipliers[grid.multipliers.length - 1] ?? 1;
  const lo = Math.min(Math.max(ch.min_mult ?? 0.5, gMin), 1);
  const hi = Math.max(Math.min(ch.max_mult ?? 1.5, gMax), 1);
  const step = ch.step ?? 0.05;
  // A range input's steps count from `min`: start them at 1 - k·step so the current plan (1)
  // is a step the thumb can land on (min 0.75 with step 0.1 would offer 0.95 and 1.05 only).
  const min = Math.round((1 - Math.floor((1 - lo) / step + 1e-9) * step) * 1e6) / 1e6;
  return { min, max: hi, step };
}

/** Keep a multiplier on the slider (and on its step, to 4 decimals). */
export function clampMultiplier(m: number, b: { min: number; max: number }): number {
  return Math.round(Math.min(b.max, Math.max(b.min, m)) * 1e4) / 1e4;
}

/** Every number of a scenario is per readout period: the grid's window total / `grid.periods`. */
export interface ScenarioChannel {
  name: string;
  label: string;
  multiplier: number;
  /** Spend per period. */
  spend: number;
  /** Contribution per period. */
  contribution: number;
  /** Contribution change against the current plan. */
  delta: number;
  zone: Zone | null;
  zoneWord: string;
}

export interface ScenarioModel {
  channels: ScenarioChannel[];
  /** The expected KPI of a period with this plan (needs the grid's baseline), with its likely range. */
  kpi: { mean: number; low: number | null; high: number | null } | null;
  kpiDelta: number;
  spendDelta: number;
  /** The periods of the grid's fit window that the numbers average over. */
  periods: number;
  /** `true` when every multiplier is 1. */
  current: boolean;
  lead: string;
}

/** The plan's sentence (per period): "This plan brings 4,100 more revenue a week than the current plan, for 2,000 more spend." */
export function scenarioLead(kpiDelta: number, spendDelta: number, d: Pick<ClientDashboard, "kpi" | "period_label">): string {
  const noun = kpiWord(d.kpi);
  const decimals = d.kpi.decimals ?? 0;
  if (Math.abs(kpiDelta) < 0.5 && Math.abs(spendDelta) < 0.5) return "This plan brings the same result as the current plan.";
  const k = `${grouped(Math.abs(kpiDelta), decimals)} ${kpiDelta >= 0 ? "more" : "less"} ${noun} a ${periodOf(d)}`;
  const s = `${grouped(Math.abs(spendDelta), 0)} ${spendDelta >= 0 ? "more" : "less"} spend`;
  return `This plan brings ${k} than the current plan, for ${s}.`;
}

/** "Orders" → "orders"; acronyms stay ("GMV"). */
export function kpiWord(kpi: Pick<KpiDef, "label">): string {
  const l = kpi.label;
  return l.slice(1) === l.slice(1).toLowerCase() ? l.toLowerCase() : l;
}

/**
 * The marginal return of channel `c` at multiplier `x`: the slope of the mean contribution over
 * spend on the grid segment that holds `x` (the one to its right at a grid point, the last one at
 * the end). The server's `_slope` (`grid_facts`), so the live zone words match the scorecard's.
 */
export function gridSlope(grid: Pick<ResponseGrid, "multipliers" | "contribution" | "spend" | "draws">, c: number, x: number): number | null {
  const xs = grid.multipliers;
  const spend = grid.spend[c];
  if (xs.length < 2 || !spend) return null;
  let i = xs.findIndex((v, k) => k >= 1 && x < v);
  if (i < 1) i = xs.length - 1;
  const draws = Math.max(1, grid.draws || grid.contribution.length);
  const mean = (k: number) => {
    let sum = 0;
    for (let d = 0; d < draws; d++) sum += grid.contribution[d]?.[c]?.[k] ?? 0;
    return sum / draws;
  };
  const ds = (spend[i] ?? 0) - (spend[i - 1] ?? 0);
  return ds ? (mean(i) - mean(i - 1)) / ds : 0;
}

/**
 * Evaluate the sliders on the grid (instant, in the browser): channel results with their live
 * zone words, the KPI change and the plan's sentence. Throws `WhatIfError` when a multiplier is
 * outside the grid (the sliders clamp, so only a stale grid can do that).
 *
 * `evaluateScenario` answers over the grid's whole fit window (`grid.periods` periods); every
 * number here is per period (window / periods), as the readout's tiles and scorecard show them
 * (the server's `whatif_result`). Returns are ratios, so the window cancels in them.
 */
export function scenarioModel(grid: ResponseGrid, d: Pick<ClientDashboard, "kpi" | "channels" | "period_label">, multipliers: Readonly<Record<string, number>>): ScenarioModel {
  const names = (d.channels ?? []).map((c) => c.name).filter((n) => grid.channels.includes(n));
  const m = Object.fromEntries(names.map((n) => [n, multipliers[n] ?? 1]));
  const res = evaluateScenario(grid, { multipliers: m });
  const cur = evaluateScenario(grid, {});
  const be = breakevenOf(d.kpi);
  const periods = grid.periods > 0 ? grid.periods : 1;
  const per = (v: number) => v / periods;
  const channels: ScenarioChannel[] = [];
  for (const def of d.channels ?? []) {
    const out = res.channels[def.name];
    const base = cur.channels[def.name];
    if (!out || !base) continue;
    const at = out.multiplier;
    const roi = out.spend > 0 ? out.contribution.mean / out.spend : null;
    const mroi = gridSlope(grid, grid.channels.indexOf(def.name), at);
    const zone = be !== null ? zoneOf(roi, mroi, be) : null;
    channels.push({
      name: def.name,
      label: def.label,
      multiplier: at,
      spend: per(out.spend),
      contribution: per(out.contribution.mean),
      delta: per(out.contribution.mean - base.contribution.mean),
      zone,
      zoneWord: zoneWord(zone),
    });
  }
  const q = (v: number | undefined) => (v === undefined ? null : per(v));
  const kpi = res.kpi ? { mean: per(res.kpi.mean), low: q(res.kpi.quantiles[0]), high: q(res.kpi.quantiles[res.kpi.quantiles.length - 1]) } : null;
  const kpiDelta = per(res.kpi_delta.mean);
  const spendDelta = per(res.spend_delta);
  return {
    channels,
    kpi,
    kpiDelta,
    spendDelta,
    periods,
    current: Object.values(m).every((v) => Math.abs(v - 1) < 1e-9),
    lead: scenarioLead(kpiDelta, spendDelta, d),
  };
}

/** "+10%" / "−20%" / "Current" for a multiplier. */
export function multiplierWords(m: number): string {
  const pct = Math.round((m - 1) * 100);
  if (pct === 0) return "Current spend";
  return `${pct > 0 ? "+" : MINUS}${Math.abs(pct)}% spend`;
}

// ------------------------------------------------------------------------------ actuals (Update numbers)

export interface Check {
  ok: boolean;
  text: string;
}

function isDateLike(v: Cell): boolean {
  return typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v) && !Number.isNaN(Date.parse(v.slice(0, 10)));
}

function isFilled(r: readonly Cell[]): boolean {
  return r.some((c) => c !== null && c !== "");
}

/** The rows that hold a value (blank rows are left out when the numbers are sent). */
export function filledRows(rows: readonly Cell[][]): Cell[][] {
  return rows.filter(isFilled);
}

/**
 * The live checks of the Update numbers tab, in plain words. The server checks everything again
 * (`ACTUALS_INVALID` with row and column pointers); these only stop an upload that cannot work.
 */
export function actualsChecks(t: Pick<ActualsTemplate, "input_table" | "kpi_column" | "max_rows">, rows: readonly Cell[][]): Check[] {
  const cols = t.input_table.columns ?? [];
  // each filled row with its number in the editor (blank rows keep their numbers there)
  const numbered = rows.map((r, i) => ({ r, n: i + 1 })).filter(({ r }) => isFilled(r));
  const filled = numbered.map(({ r }) => r);
  const rowNo = (i: number) => numbered[i]?.n ?? i + 1;
  const max = t.max_rows ?? 520;
  const kpiAt = cols.findIndex((c) => c.name === t.kpi_column);
  const kpiLabel = t.kpi_column;
  const checks: Check[] = [];

  checks.push(filled.length === 0 ? { ok: false, text: "Add at least one row." } : filled.length > max ? { ok: false, text: `Use ${max} rows or fewer. You have ${filled.length}.` } : { ok: true, text: `${filled.length} ${plural("row", filled.length)} to send.` });

  const kpiValues = kpiAt < 0 ? [] : filled.map((r) => r[kpiAt] ?? null);
  const kpiNums = kpiValues.filter((v): v is number => typeof v === "number");
  if (kpiAt < 0) checks.push({ ok: false, text: `The template has no column ${kpiLabel}.` });
  else if (!kpiNums.length) checks.push({ ok: false, text: `Enter at least one number in the column ${kpiLabel}.` });
  else checks.push({ ok: true, text: `The column ${kpiLabel} has ${kpiNums.length} ${plural("number", kpiNums.length)}.` });

  const notNumber: string[] = [];
  const negative: string[] = [];
  cols.forEach((c, j) => {
    if (c.role === "coord") return;
    filled.forEach((r, i) => {
      const v = r[j] ?? null;
      if (v === null || v === "") return;
      if (typeof v !== "number") notNumber.push(`row ${rowNo(i)}, ${c.name}`);
      else if (v < 0) negative.push(`row ${rowNo(i)}, ${c.name}`);
    });
  });
  checks.push(notNumber.length ? { ok: false, text: `Use numbers only. Check ${notNumber.slice(0, 3).join("; ")}${notNumber.length > 3 ? ` and ${notNumber.length - 3} more` : ""}.` } : { ok: true, text: "All values are numbers." });
  if (negative.length) checks.push({ ok: false, text: `Numbers must be 0 or more. Check ${negative.slice(0, 3).join("; ")}${negative.length > 3 ? ` and ${negative.length - 3} more` : ""}.` });

  const dim = t.input_table.horizon_dim;
  const dimAt = dim ? cols.findIndex((c) => c.name === dim) : -1;
  if (dimAt >= 0) {
    const labels = filled.map((r) => r[dimAt] ?? null);
    const missing = labels.findIndex((v) => v === null || v === "");
    const freq = cols[dimAt]?.freq;
    const bad = freq ? labels.findIndex((v) => v !== null && v !== "" && !isDateLike(v)) : -1;
    // A row is one combination of the coordinate columns: a long table (an MMM's week x
    // channel) repeats each period once per channel, which is not a duplicate.
    const coordAt = cols.map((c, j) => (c.role === "coord" ? j : -1)).filter((j) => j >= 0);
    const others = coordAt.filter((j) => j !== dimAt);
    const keys = filled.map((r) => JSON.stringify(coordAt.map((j) => String(r[j] ?? ""))));
    const dup = keys.findIndex((k, i) => keys.indexOf(k) !== i);
    const what = (i: number) => [`${dim} ${String(labels[i])}`, ...others.map((j) => `${cols[j]?.name} ${String(filled[i]?.[j] ?? "")}`)].join(", ");
    if (missing >= 0) checks.push({ ok: false, text: `Row ${rowNo(missing)} has no ${dim}.` });
    else if (bad >= 0) checks.push({ ok: false, text: `Row ${rowNo(bad)}: write the ${dim} as yyyy-MM-dd.` });
    else if (dup >= 0) checks.push({ ok: false, text: `The ${what(dup)} occurs two times.` });
    else checks.push({ ok: true, text: others.length ? `Each row has its own ${[dim, ...others.map((j) => cols[j]?.name)].join(" and ")}.` : `Each row has its own ${dim}.` });
  }
  return checks;
}

export function checksPass(checks: readonly Check[]): boolean {
  return checks.every((c) => c.ok);
}

// ------------------------------------------------------------------------------ "Download summary (.csv)"

/** A plain signed number ("+3.2%", "-1,200", "+0.4 pts"): data, never a formula. */
const SIGNED_NUMBER = /^[+-]\d[\d,]*(?:\.\d+)?(?:\s?%| pts)?$/;

export function csvCell(v: string): string {
  // Spreadsheet formula injection: a cell that starts with = + - @ is read as text, except a
  // plain signed number (a positive change, "+3.2%"), which would show a literal apostrophe.
  const safe = /^[=+\-@\t\r]/.test(v) && !SIGNED_NUMBER.test(v) ? `'${v}` : v;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** The latest readout as a small CSV: the tiles, then the channel scorecard. */
export function summaryCsv(d: Pick<ClientDashboard, "title" | "kpi" | "channels" | "tiles" | "period_label">, r: Readout | null, timeZone?: string): string {
  const lines: string[][] = [[d.title], [metaLine(r, periodOf(d), timeZone)]];
  const snap = r?.snapshot ?? null;
  if (snap) {
    lines.push([snap.lead], [], ["Measure", "Value", "Change", "Note"]);
    for (const t of tileViews(snap, d.kpi, d.tiles ?? [])) lines.push([t.label, t.value, t.delta ?? "", t.caption]);
    const rows = channelRows(snap, d);
    if (rows.length) {
      lines.push([], SCORECARD_COLUMNS.map((c) => c.label));
      for (const row of rows) lines.push(scorecardCells(row).map((c) => c.text));
    }
  }
  return `${lines.map((l) => l.map(csvCell).join(",")).join("\r\n")}\r\n`;
}

/** A file name from the title: "weekly-media-readout-2026-10-18.csv". */
export function summaryFileName(title: string, r: Pick<ReadoutHeader, "period" | "created_at"> | null): string {
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "readout";
  const day = r?.period?.[1] ?? r?.created_at.slice(0, 10) ?? "";
  return `${slug}${day ? `-${day}` : ""}.csv`;
}
