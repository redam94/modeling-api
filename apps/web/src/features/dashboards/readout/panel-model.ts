/**
 * The pure model of the layout 2 readout (v0.17, plan §3.7): the section names, what each
 * panel block says in plain words (tiles, ranges, the "approximate" tag), the summary CSV and
 * the one-time upgrade note. What the inputs send and give back (the checks of a tried case, a
 * batch file and a row-matched upload; predictions, batch summaries and accuracy in words) is
 * in `panels/results-model.ts`, which loads only with the lazy part of the readout.
 *
 * Nothing reads the network or the DOM; the one exception is the upgrade note memory, which
 * reads browser storage inside try/catch and works without it.
 */
import { fixed, grouped } from "@/components/ods/format";

import { visibleSections } from "../client";
import type {
  ClientDashboard,
  DashboardClient,
  DashboardClientV2,
  DeltaDir,
  KpiBlock,
  KpiDef,
  KpiValue,
  PanelBlock,
  PrincipalKind,
  Readout,
  SectionKey,
  SubjectDef,
  TabKey,
} from "../types";
import { csvCell, deltaWords, formatValue, metaLine, periodOf, type Tag } from "./readout-model";

type Num = number | null | undefined;
const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);

// ------------------------------------------------------------------------------ sections

export const SECTION_LABEL: Record<SectionKey, string> = {
  overview: "Overview",
  try: "Try a case",
  accuracy: "Check accuracy",
  batch: "Score a file",
  history: "History",
};

/** The sections to show: the server's list (else the inputs'), `overview` first, no duplicates. */
export function pageSections(d: Pick<ClientDashboard, "sections" | "inputs">): SectionKey[] {
  const listed = d.sections?.length ? d.sections : visibleSections(d.inputs ?? {});
  return ["overview", ...new Set(listed.filter((s) => s !== "overview"))];
}

/** A layout 1 tab name (a `?tab=` link from before the upgrade) as the layout 2 section. */
export function sectionOfTab(tab: TabKey | undefined): SectionKey | undefined {
  switch (tab) {
    case undefined:
      return undefined;
    case "update":
      return "accuracy";
    case "history":
      return "history";
    case "scenarios":
      return "try";
    default:
      return "overview";
  }
}

/** The client as a layout 2 one (scenario rows, batch, downloads, pictures), or `null`. */
export function asClientV2(client: DashboardClient): DashboardClientV2 | null {
  const c = client as Partial<DashboardClientV2>;
  return typeof c.predict === "function" && typeof c.submitBatch === "function" && typeof c.asset === "function" ? (client as DashboardClientV2) : null;
}

// ------------------------------------------------------------------------------ numbers

/** The subject's number form (currency, decimals) on the layout 1 KPI the dashboard carries. */
export function subjectKpi(d: Pick<ClientDashboard, "kpi" | "subject">): KpiDef {
  const s: SubjectDef | null | undefined = d.subject;
  if (!s) return d.kpi;
  return { ...d.kpi, currency: s.currency ?? d.kpi.currency ?? null, decimals: s.decimals ?? d.kpi.decimals ?? 0 };
}

/** A value in a tile's unit, in the subject's number form when the unit is the subject's. */
export function unitText(v: Num, unit: KpiValue["unit"], d: Pick<ClientDashboard, "kpi" | "subject">): string {
  const kpi = subjectKpi(d);
  const own = !d.subject || unit === d.subject.unit || unit === "currency";
  return formatValue(v, unit, own ? kpi : { currency: kpi.currency ?? null, decimals: unit === "number" || unit === "count" ? 2 : 0 });
}

/** A plain number of a chart axis or table cell: grouped, with the decimals the size needs. */
export function plainNumber(v: number): string {
  const a = Math.abs(v);
  if (a >= 100 || Number.isInteger(v)) return grouped(v, 0);
  if (a >= 1) return grouped(v, 2);
  return fixed(v, 3);
}

/** "likely between 1,200 and 1,900" (`null` without both ends). */
export function rangeWords(low: Num, high: Num, format: (v: number) => string): string | null {
  return isNum(low) && isNum(high) ? `Likely between ${format(low)} and ${format(high)}` : null;
}

export const APPROXIMATE_TAG: Tag = { label: "Approximate", tone: "neutral" };
/** The `approximate` glossary text (`mapi_spec.dashboards.GLOSSARY`). */
export const APPROXIMATE_TEXT = "This value is approximate. The model gives one value per case without a range, so the total does not show the uncertainty.";

export function blockTags(block: Pick<PanelBlock, "approximate">): Tag[] {
  return block.approximate ? [APPROXIMATE_TAG] : [];
}

export interface PanelTileView {
  id: string;
  label: string;
  value: string;
  delta: string | null;
  deltaDir: DeltaDir;
  caption: string;
  approximate: boolean;
}

/** One KPI block as a tile: the value, its likely range and the change against the readout before. */
export function panelTile(block: KpiBlock, d: Pick<ClientDashboard, "kpi" | "subject">): PanelTileView {
  const t = block.tile;
  const fmt = (v: number) => unitText(v, t.unit, d);
  const range = rangeWords(t.low, t.high, fmt);
  const caption = [range ? `${range}.` : null, t.caption || null, block.approximate || t.approximate ? "Approximate." : null].filter(Boolean).join(" ");
  return {
    id: block.panel_id,
    label: t.label || block.title,
    value: unitText(t.value, t.unit, d),
    delta: deltaWords(t),
    deltaDir: t.delta_dir ?? "flat",
    caption,
    approximate: Boolean(block.approximate || t.approximate),
  };
}

/** The KPI blocks of a snapshot in panel order, and every other block after them. */
export function splitBlocks(blocks: readonly PanelBlock[]): { tiles: KpiBlock[]; rest: PanelBlock[] } {
  const tiles: KpiBlock[] = [];
  const rest: PanelBlock[] = [];
  for (const b of blocks) {
    if (b.kind === "kpi") tiles.push(b);
    else rest.push(b);
  }
  return { tiles, rest };
}

/** Is a series block the subject line the media planner already draws (its default "fan")? */
export function isPlannerDuplicate(block: PanelBlock): boolean {
  return block.kind === "series" && block.panel_id === "fan";
}

// ------------------------------------------------------------------------------ "Download summary (.csv)"

/** The latest layout 2 readout as a small CSV: the lead, then the KPI tiles. */
export function summaryCsvV2(d: Pick<ClientDashboard, "title" | "kpi" | "subject" | "period_label">, r: Readout | null, timeZone?: string): string {
  const lines: string[][] = [[d.title], [metaLine(r, periodOf(d), timeZone)]];
  const snap = r?.snapshot ?? null;
  if (snap) {
    lines.push([snap.lead], [], ["Measure", "Value", "Change", "Note"]);
    for (const b of splitBlocks(snap.panels ?? []).tiles) {
      const t = panelTile(b, d);
      lines.push([t.label, t.value, t.delta ?? "", t.caption]);
    }
  }
  return `${lines.map((l) => l.map(csvCell).join(",")).join("\r\n")}\r\n`;
}

// ------------------------------------------------------------------------------ the one-time upgrade note

const NOTE_KEY = "mapi.dashboards.upgradeNote.";
/** Share links keep the dismissal for the open page only (plan §10.1: in memory or the session). */
const MEMORY = new Map<string, string>();

function storageOf(kind: PrincipalKind): Storage | null {
  try {
    return kind === "share" ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

/** Has this viewer dismissed this note on this dashboard? (Storage that throws counts as no.) */
export function upgradeNoteDismissed(kind: PrincipalKind, dashboardId: string, note: string): boolean {
  const key = NOTE_KEY + dashboardId;
  if (MEMORY.get(key) === note) return true;
  try {
    return storageOf(kind)?.getItem(key) === note;
  } catch {
    return false;
  }
}

/** Remember that this viewer dismissed the note (for this note text: a new note shows again). */
export function dismissUpgradeNote(kind: PrincipalKind, dashboardId: string, note: string): void {
  const key = NOTE_KEY + dashboardId;
  MEMORY.set(key, note);
  try {
    storageOf(kind)?.setItem(key, note);
  } catch {
    // Storage is off (a private window, blocked site data): the note stays dismissed for this page.
  }
}

/** For tests: forget the in-memory dismissals. */
export function resetUpgradeNoteMemory(): void {
  MEMORY.clear();
}

