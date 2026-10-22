/**
 * The plot recipes of the catalog as cards: an example render of each plot (made on
 * synthetic data by the describe run), its plain-language summary and what it needs. A card
 * opens the full About card with a larger, interactive example.
 *
 * v0.18: each card has "Try it" (the recipe playground, WP-T5, in its own lazy chunk) and
 * "Make a chart template" (Make your own › Charts, starting from this recipe). A plugin's pack
 * shows its `pack_label` when the app has no name for it. "Add your own" closes the page.
 */
import { Link } from "@tanstack/react-router";
import { Palette, Play, Search } from "lucide-react";
import { lazy, Suspense, useMemo, useState } from "react";

import type { PlotCatalogEntry } from "@/api/types";
import { EmptyState, KeyValues } from "@/components/common";
import { AddYourOwn } from "@/components/extend/add-your-own";
import { Button } from "@/components/ui/button";
import { Badge, Input } from "@/components/ui/primitives";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { AboutCard, DifficultyChip } from "@/features/about/about-card";
import { cn } from "@/lib/utils";

// The recipe playground loads only when someone presses "Try it" (`recipe-playground-*` chunk).
const RecipePlaygroundDialog = lazy(() => import("./recipe-playground"));

import { PlotExample } from "./plot-example";

const PERSONA_LABEL: Record<string, string> = { guided: "Guided", expert: "Expert", both: "Guided and Expert" };

/** The pack of a recipe id (`core`, `mmm`, `ml`, `sci`, or a plugin's prefix). */
export function plotPack(id: string): string {
  return id.split(".", 1)[0] ?? id;
}

const PACK_LABEL: Record<string, string> = { core: "All models", mmm: "Marketing mix", ml: "Machine learning", sci: "Science", net: "Networks" };

/**
 * The name of a pack: the app's own name for a built-in pack, else the `pack_label` a recipe of
 * that pack publishes (a plugin's), else the namespace itself.
 */
export function packLabel(pack: string, plots: readonly Pick<PlotCatalogEntry, "id" | "pack_label">[] = []): string {
  const own = PACK_LABEL[pack];
  if (own) return own;
  const published = plots.find((p) => plotPack(p.id) === pack && p.pack_label)?.pack_label;
  return published ?? pack;
}

/** Make your own › Charts, a new chart template from `recipeId`. */
function ChartTemplateLink({ recipeId, title, className, children }: { recipeId: string; title: string; className?: string; children: React.ReactNode }) {
  return (
    <Link to="/library" search={{ tab: "charts", new: "chart", source: recipeId }} className={className} aria-label={`Make a chart template from ${title}`}>
      {children}
    </Link>
  );
}

const PILL_SM = "h-auto min-h-9 rounded-full px-4 text-xs";

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

function PlotCard({ plot, pack, onOpen, onTry }: { plot: PlotCatalogEntry; pack: string; onOpen: () => void; onTry: () => void }) {
  const title = plot.about?.title ?? plot.title;
  const summary = plot.about?.summary_plain ?? plot.summary ?? "";
  return (
    <article className="flex h-full flex-col overflow-hidden rounded-2xl border bg-card transition-colors hover:border-primary">
      <button
        type="button"
        onClick={onOpen}
        aria-label={`${title}: ${summary}`}
        data-testid="plot-card"
        data-plot={plot.id}
        className="focus-ring group flex flex-1 flex-col text-left"
      >
        <div className="border-b bg-muted/30 p-2">
          <PlotExample id={plot.id} version={plot.version ?? "1.0.0"} title={title} height={190} preview />
        </div>
        <span className="flex flex-1 flex-col gap-2 p-5">
          <span className="flex flex-wrap items-start justify-between gap-2">
            <span className="min-w-0">
              <span className="block text-xs font-semibold text-muted-foreground">{pack}</span>
              <span className="block font-display text-xl leading-tight font-bold tracking-[-0.02em]">{title}</span>
            </span>
            {plot.about ? <DifficultyChip difficulty={plot.about.difficulty} /> : null}
          </span>
          {summary ? <span className="line-clamp-3 text-sm text-muted-foreground">{summary}</span> : null}
          <span className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
            {(plot.requires_groups ?? []).map((g) => (
              <Badge key={g} variant="outline" className="font-mono text-[10px]">
                {g}
              </Badge>
            ))}
            <span className="truncate font-mono text-xs text-hint">{plot.id}</span>
          </span>
        </span>
      </button>
      <div className="flex flex-wrap items-center gap-2 border-t border-dashed border-border-strong px-5 py-3">
        <Button size="sm" className={PILL_SM} onClick={onTry} data-tour="catalog.recipe.try" aria-label={`Try ${title}`}>
          <Play /> Try it
        </Button>
        <Button size="sm" variant="ghost" className={cn(PILL_SM, "bg-muted text-foreground hover:bg-surface-hover")} asChild>
          <ChartTemplateLink recipeId={plot.id} title={title}>
            <Palette /> Make a chart template
          </ChartTemplateLink>
        </Button>
      </div>
    </article>
  );
}

/** The full card of one recipe: About, the example (interactive) and technical details. */
export function PlotDetail({ plot, onClose, onTry }: { plot: PlotCatalogEntry; onClose: () => void; onTry?: (() => void) | undefined }) {
  const version = plot.version ?? "1.0.0";
  const title = plot.about?.title ?? plot.title;
  const example = (
    <section aria-label="Example" className="grid gap-1.5">
      <PlotExample id={plot.id} version={version} title={title} height={360} className="rounded-lg border p-2" />
      <p className="text-xs text-muted-foreground">An example made from synthetic data. Your model's plot uses your data.</p>
    </section>
  );
  const actions = (
    <>
      {onTry ? (
        <Button size="sm" className={PILL_SM} onClick={onTry}>
          <Play /> Try it
        </Button>
      ) : null}
      <Button size="sm" variant="ghost" className={cn(PILL_SM, "bg-muted text-foreground hover:bg-surface-hover")} asChild>
        <ChartTemplateLink recipeId={plot.id} title={title}>
          <Palette /> Make a chart template
        </ChartTemplateLink>
      </Button>
    </>
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
        {plot.about ? <AboutCard about={plot.about} kind="plot" id={plot.id} as="h2" media={example} actions={actions} /> : example}
        {plot.about ? null : <div className="flex flex-wrap gap-2">{actions}</div>}
        <h3 className="text-sm font-semibold">Technical details</h3>
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
  const [trying, setTrying] = useState<PlotCatalogEntry | null>(null);
  const latest = useMemo(() => latestPlots(plots), [plots]);
  const packs = useMemo(() => [...new Set(latest.map((p) => plotPack(p.id)))], [latest]);
  const items = useMemo(() => filterPlots(latest, text, pack), [latest, text, pack]);
  const tryPlot = (p: PlotCatalogEntry) => {
    setOpen(null);
    setTrying(p);
  };
  return (
    <div className="grid gap-6">
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Plot pack" className="flex flex-wrap gap-2">
          {["all", ...packs].map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={pack === k}
              onClick={() => setPack(k)}
              className="focus-ring inline-flex min-h-10 items-center gap-1.5 rounded-full border-[1.5px] border-border-strong bg-card px-4 text-sm font-semibold transition-colors hover:border-primary aria-pressed:border-foreground aria-pressed:bg-foreground aria-pressed:text-background"
            >
              {k === "all" ? "All plots" : packLabel(k, latest)}
              <span className="font-medium tabular opacity-65">{k === "all" ? latest.length : latest.filter((p) => plotPack(p.id) === k).length}</span>
            </button>
          ))}
        </div>
        <div className="relative ml-auto min-w-[min(14rem,100%)] flex-1 sm:max-w-xs">
          <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input aria-label="Search the plots" placeholder="For example forecast, residuals or budget" value={text} onChange={(e) => setText(e.target.value)} className="rounded-full pl-9" />
        </div>
      </div>
      {items.length ? (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Plot recipes">
          {items.map((p) => (
            <li key={p.id}>
              <PlotCard plot={p} pack={packLabel(plotPack(p.id), latest)} onOpen={() => setOpen(p)} onTry={() => tryPlot(p)} />
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState icon={<Search />} title="No plot matches">
          Type a different word, or select all plots.
        </EmptyState>
      )}
      <AddYourOwn kind="plot" />
      {open ? <PlotDetail plot={open} onClose={() => setOpen(null)} onTry={() => tryPlot(open)} /> : null}
      {trying ? (
        <Suspense fallback={null}>
          <RecipePlaygroundDialog plot={trying} open onOpenChange={(o) => (o ? undefined : setTrying(null))} />
        </Suspense>
      ) : null}
    </div>
  );
}
