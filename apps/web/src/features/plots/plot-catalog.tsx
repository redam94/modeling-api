/**
 * The plot recipes of the catalog as cards: an example render of each plot (made on
 * synthetic data by the describe run), its plain-language summary and what it needs. A card
 * opens the full About card with a larger, interactive example.
 */
import { Search } from "lucide-react";
import { useMemo, useState } from "react";

import type { PlotCatalogEntry } from "@/api/types";
import { EmptyState, KeyValues } from "@/components/common";
import { Badge, Input } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { AboutCard, DifficultyBadge } from "@/features/about/about-card";
import { cn } from "@/lib/utils";

import { PlotExample } from "./plot-example";

const PERSONA_LABEL: Record<string, string> = { guided: "Guided", expert: "Expert", both: "Guided and Expert" };

/** The pack of a recipe id (`core`, `mmm`, `ml`, `sci`, or a plugin's prefix). */
export function plotPack(id: string): string {
  return id.split(".", 1)[0] ?? id;
}

const PACK_LABEL: Record<string, string> = { core: "All models", mmm: "Marketing mix", ml: "Machine learning", sci: "Science", net: "Networks" };

/** Latest non-yanked version of each recipe id, sorted by pack then title. */
export function latestPlots(plots: readonly PlotCatalogEntry[]): PlotCatalogEntry[] {
  const best = new Map<string, PlotCatalogEntry>();
  for (const p of plots) {
    if (p.yanked) continue;
    const cur = best.get(p.id);
    if (!cur || compareVersions(p.version ?? "1.0.0", cur.version ?? "1.0.0") > 0) best.set(p.id, p);
  }
  const packRank = (id: string) => {
    const i = Object.keys(PACK_LABEL).indexOf(plotPack(id));
    return i < 0 ? 99 : i;
  };
  return [...best.values()].sort((a, b) => packRank(a.id) - packRank(b.id) || a.title.localeCompare(b.title));
}

function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d) return d;
  }
  return 0;
}

/** Recipes matching every word of `text` (id, title, summary, About lists). */
export function filterPlots(plots: readonly PlotCatalogEntry[], text: string, pack: string): PlotCatalogEntry[] {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  return plots.filter((p) => {
    if (pack !== "all" && plotPack(p.id) !== pack) return false;
    if (!words.length) return true;
    const a = p.about;
    const hay = [p.id, p.title, p.summary ?? "", a?.title ?? "", a?.summary_plain ?? "", ...(a?.use_when ?? []), ...(a?.outputs ?? [])].join(" ").toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

function PlotCard({ plot, onOpen }: { plot: PlotCatalogEntry; onOpen: () => void }) {
  const title = plot.about?.title ?? plot.title;
  const summary = plot.about?.summary_plain ?? plot.summary ?? "";
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`${title}: ${summary}`}
      data-testid="plot-card"
      data-plot={plot.id}
      className="group flex h-full flex-col overflow-hidden rounded-xl border bg-card text-left shadow-[0_1px_0_0_rgb(0_0_0/0.03)] transition-colors hover:border-primary/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
    >
      <div className="border-b bg-muted/20 p-2">
        <PlotExample id={plot.id} version={plot.version ?? "1.0.0"} title={title} height={190} preview />
      </div>
      <span className="flex flex-1 flex-col gap-2 p-4">
        <span className="flex flex-wrap items-start justify-between gap-2">
          <span className="min-w-0">
            <span className="block text-[11px] font-medium text-muted-foreground">{PACK_LABEL[plotPack(plot.id)] ?? plotPack(plot.id)}</span>
            <span className="block font-semibold tracking-tight">{title}</span>
          </span>
          {plot.about ? <DifficultyBadge difficulty={plot.about.difficulty} /> : null}
        </span>
        {summary ? <span className="line-clamp-3 text-[13px] text-muted-foreground">{summary}</span> : null}
        <span className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
          {(plot.requires_groups ?? []).map((g) => (
            <Badge key={g} variant="outline" className="font-mono text-[10px]">
              {g}
            </Badge>
          ))}
          <span className="truncate font-mono text-[11px] text-muted-foreground">{plot.id}</span>
        </span>
      </span>
    </button>
  );
}

/** The full card of one recipe: About, the example (interactive) and technical details. */
export function PlotDetail({ plot, onClose }: { plot: PlotCatalogEntry; onClose: () => void }) {
  const version = plot.version ?? "1.0.0";
  const title = plot.about?.title ?? plot.title;
  const example = (
    <section aria-label="Example" className="grid gap-1.5">
      <PlotExample id={plot.id} version={version} title={title} height={360} className="rounded-lg border p-2" />
      <p className="text-xs text-muted-foreground">An example made from synthetic data. Your model's plot uses your data.</p>
    </section>
  );
  const technical = (
    <KeyValues
      items={[
        ["Recipe", `${plot.id}@${version}`],
        ["Shown in", PERSONA_LABEL[plot.persona ?? "both"] ?? String(plot.persona)],
        ["Needs groups", (plot.requires_groups ?? []).join(", ") || "—"],
        ["Needs representation", (plot.requires_representation ?? []).join(", ") || "any"],
      ]}
    />
  );
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle className={cn(plot.about && "sr-only")}>{title}</DialogTitle>
        <DialogDescription className={cn(plot.about && "sr-only")}>{plot.about?.summary_plain ?? plot.summary ?? "A plot recipe of the catalog."}</DialogDescription>
        {plot.about ? (
          <AboutCard about={plot.about} kind="plot" id={plot.id} as="h2" media={example} />
        ) : (
          example
        )}
        <h3 className="text-[13px] font-semibold">Technical details</h3>
        {technical}
      </DialogContent>
    </Dialog>
  );
}

/** Every plot recipe as a card with its example. */
export function PlotCatalogGrid({ plots }: { plots: readonly PlotCatalogEntry[] }) {
  const [text, setText] = useState("");
  const [pack, setPack] = useState("all");
  const [open, setOpen] = useState<PlotCatalogEntry | null>(null);
  const latest = useMemo(() => latestPlots(plots), [plots]);
  const packs = useMemo(() => [...new Set(latest.map((p) => plotPack(p.id)))], [latest]);
  const items = useMemo(() => filterPlots(latest, text, pack), [latest, text, pack]);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Plot pack" className="flex flex-wrap gap-1.5">
          {["all", ...packs].map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={pack === k}
              onClick={() => setPack(k)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring",
                pack === k && "border-primary/60 bg-accent text-accent-foreground",
              )}
            >
              {k === "all" ? "All plots" : (PACK_LABEL[k] ?? k)}
              <span className="text-muted-foreground tabular">{k === "all" ? latest.length : latest.filter((p) => plotPack(p.id) === k).length}</span>
            </button>
          ))}
        </div>
        <div className="relative ml-auto min-w-[14rem] flex-1 sm:max-w-xs">
          <Search aria-hidden className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
          <Input aria-label="Search the plots" placeholder="For example forecast, residuals or budget" value={text} onChange={(e) => setText(e.target.value)} className="pl-8" />
        </div>
      </div>
      {items.length ? (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Plot recipes">
          {items.map((p) => (
            <li key={p.id}>
              <PlotCard plot={p} onOpen={() => setOpen(p)} />
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState icon={<Search />} title="No plot matches">
          Type a different word, or select all plots.
        </EmptyState>
      )}
      {open ? <PlotDetail plot={open} onClose={() => setOpen(null)} /> : null}
    </div>
  );
}
