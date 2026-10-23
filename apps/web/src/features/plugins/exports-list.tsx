/**
 * What an upload adds (`PluginExports`): its models, prep steps, charts, workflow steps and
 * templates, each with the untrusted title as text, the id, and the "Uploaded by" chip (D19).
 * Plus the reviewer's diff against the last approved version (D13: an approval never carries
 * over to different exports).
 */
import { Upload } from "lucide-react";

import { cn } from "@/lib/utils";

import { displayText, EXPORT_LABEL } from "./states";
import type { ExportKind, PluginExports } from "./types";
import { EXPORT_KINDS } from "./types";

export interface ExportRow {
  kind: ExportKind;
  id: string;
  version: string;
  title: string;
  summary: string;
  /** Canonical JSON of the entry (to tell "changed" from "same" in the diff). */
  fingerprint: string;
}

/** Every export, in kind order. */
export function exportRows(e: PluginExports | null | undefined): ExportRow[] {
  if (!e) return [];
  const rows: ExportRow[] = [];
  const add = (kind: ExportKind, id: string, version: string | null | undefined, title: string | null | undefined, summary: string | null | undefined, raw: unknown) =>
    rows.push({ kind, id, version: version ?? "", title: title ?? id, summary: summary ?? "", fingerprint: JSON.stringify(raw) });
  for (const m of e.models ?? []) add("model", m.manifest.id, m.manifest.version, m.manifest.about?.title, m.manifest.about?.summary_plain, m);
  for (const s of e.steps ?? []) add("step", s.id, s.version, s.about?.title, s.about?.summary_plain ?? s.summary, s);
  for (const p of e.plots ?? []) add("plot", p.id, p.version, p.about?.title ?? p.title, p.about?.summary_plain, p);
  for (const n of e.workflow_nodes ?? []) add("node", n.id, n.version, n.about?.title, n.about?.summary_plain, n);
  for (const t of e.workflow_templates ?? []) add("template", t.id, t.version, t.about?.title, t.about?.summary_plain, t);
  return rows;
}

export type DiffMark = "added" | "removed" | "changed" | "same";

export interface DiffRow extends ExportRow {
  mark: DiffMark;
}

/** `current` against `previous` (by kind and id); removed rows come last. */
export function exportsDiff(current: PluginExports | null | undefined, previous: PluginExports | null | undefined): DiffRow[] {
  const prev = new Map(exportRows(previous).map((r) => [`${r.kind}:${r.id}`, r]));
  const out: DiffRow[] = [];
  for (const r of exportRows(current)) {
    const before = prev.get(`${r.kind}:${r.id}`);
    prev.delete(`${r.kind}:${r.id}`);
    out.push({ ...r, mark: !before ? "added" : before.fingerprint === r.fingerprint ? "same" : "changed" });
  }
  for (const r of prev.values()) out.push({ ...r, mark: "removed" });
  return out;
}

const MARK: Record<DiffMark, { label: string; cls: string }> = {
  added: { label: "New", cls: "bg-highlight-soft text-highlight-ink" },
  changed: { label: "Changed", cls: "bg-attention-soft text-attention" },
  removed: { label: "Removed", cls: "bg-muted text-muted-foreground line-through" },
  same: { label: "Same", cls: "bg-muted text-muted-foreground" },
};

const CHIP = "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap";

/** The always-visible "Uploaded by" chip (D19). */
export function UploadedByChip({ name }: { name: string }) {
  return (
    <span className={cn(CHIP, "bg-accent text-accent-foreground")} data-testid="uploaded-by-chip">
      <Upload aria-hidden className="size-3" />
      Uploaded by {name}
    </span>
  );
}

function RowList({ rows, uploader, label }: { rows: (ExportRow | DiffRow)[]; uploader: string; label: string }) {
  return (
    <ul className="grid gap-0" aria-label={label}>
      {rows.map((r) => (
        <li key={`${r.kind}:${r.id}:${"mark" in r ? r.mark : ""}`} className="grid gap-1 border-t py-3 first:border-t-0 first:pt-0" data-export={r.id}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-base leading-snug font-bold [overflow-wrap:anywhere]">{displayText(r.title)}</span>
            {"mark" in r ? <span className={cn(CHIP, MARK[r.mark].cls)}>{MARK[r.mark].label}</span> : null}
          </div>
          <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
            {r.id}
            {r.version ? `@${r.version}` : ""}
          </p>
          {r.summary ? <p className="line-clamp-2 text-sm text-muted-foreground [overflow-wrap:anywhere]">{displayText(r.summary)}</p> : null}
          <div className="flex flex-wrap gap-1.5">
            <span className={cn(CHIP, "bg-muted text-muted-foreground")}>{EXPORT_LABEL[r.kind][0]}</span>
            <UploadedByChip name={uploader} />
          </div>
        </li>
      ))}
    </ul>
  );
}

export function ExportsList({ exports, uploader }: { exports: PluginExports | null | undefined; uploader: string }) {
  const rows = exportRows(exports);
  if (!rows.length) return null;
  const kinds = EXPORT_KINDS.filter((k) => rows.some((r) => r.kind === k));
  return (
    <section data-testid="plugin-exports" aria-labelledby="plugin-exports-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 rounded-2xl border bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-exports-title" className="font-display text-lg leading-tight font-bold">
          What it adds
        </h3>
        <p className="text-sm text-muted-foreground">
          {kinds.map((k) => `${rows.filter((r) => r.kind === k).length} ${EXPORT_LABEL[k][rows.filter((r) => r.kind === k).length === 1 ? 0 : 1]}`).join(", ")}
        </p>
      </div>
      <RowList rows={rows} uploader={uploader} label="What the upload adds" />
    </section>
  );
}

/** The reviewer's diff against the last approved version (or "first version"). */
export function ExportsDiff({
  current,
  previous,
  previousVersion,
  uploader,
}: {
  current: PluginExports | null | undefined;
  previous: PluginExports | null | undefined;
  previousVersion: string | null;
  uploader: string;
}) {
  const rows = previousVersion ? exportsDiff(current, previous) : exportRows(current).map((r) => ({ ...r, mark: "added" as const }));
  const changed = rows.filter((r) => r.mark !== "same").length;
  return (
    <section data-testid="plugin-exports-diff" aria-labelledby="plugin-diff-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 rounded-2xl border bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-diff-title" className="font-display text-lg leading-tight font-bold">
          What changed
        </h3>
        <p className="text-sm text-muted-foreground">
          {previousVersion
            ? `Compared with ${previousVersion}, the last approved version: ${changed === 0 ? "the exports are the same." : `${changed} ${changed === 1 ? "entry is" : "entries are"} new, changed or removed.`}`
            : "No version of this plugin was approved before, so everything is new."}
        </p>
      </div>
      {rows.length ? <RowList rows={rows} uploader={uploader} label="Changes against the last approved version" /> : <p className="text-sm text-muted-foreground">This version adds nothing.</p>}
    </section>
  );
}
