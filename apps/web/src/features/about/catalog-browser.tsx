/**
 * Every item a user can pick, by its About card: models, preparation steps, splits, Guided
 * analyses, workflow templates, workflow nodes and governance checks
 * (`GET /v1/catalog/abouts`), filterable by kind, difficulty and text. The text is the page's
 * `?q=` (v0.21: the list toolbar).
 *
 * v0.18 (§3.4): a card grid with kind glyphs and "good for" / difficulty / runtime
 * chips. The marketing-mix model is featured on the page's one ink block, next to the what-if
 * playground on example data (WP-T5's `catalog-playground`, lazy).
 */
import { Sparkles } from "lucide-react";
import { useMemo } from "react";
import { Link } from "@tanstack/react-router";

import { useCatalog } from "@/api/queries";
import { EmptyState, ErrorNotice, LoadingRows } from "@/components/common";
import { EmptyArt } from "@/components/brand/empty-art";
import { CatalogGlyph, glyphKind } from "@/components/illustrations";
import { ListNoMatches, ListToolbar, matchesQuery } from "@/components/list-toolbar";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/primitives";
import { CatalogPlayground } from "@/features/onboarding/catalog-playground";
import { PlotExample } from "@/features/plots/plot-example";
import { cn } from "@/lib/utils";

import { AboutDialog, AboutTile, DifficultyChip, RuntimeChip } from "./about-card";
import { useAboutIndex } from "./api";
import { StartButton } from "./chooser";
import { aboutKey, KIND_META, KIND_ORDER, parseAboutKey } from "./labels";
import type { AboutKind, CatalogAbout, Difficulty } from "./types";

/**
 * Items matching a text (title, id, summary, use-when) and filters, in catalog order. A title
 * or an id is also found as the global search finds it (the letters of a word in order).
 */
export function filterAbouts(items: readonly CatalogAbout[], f: { kind: AboutKind | "all"; difficulty: Difficulty | "all"; text: string }): CatalogAbout[] {
  const words = f.text.toLowerCase().split(/\s+/).filter(Boolean);
  const rank = (k: AboutKind) => (KIND_ORDER.includes(k) ? KIND_ORDER.indexOf(k) : KIND_ORDER.length);
  return items
    .filter((a) => (f.kind === "all" || a.kind === f.kind) && (f.difficulty === "all" || a.about.difficulty === f.difficulty))
    .filter((a) => {
      if (!words.length) return true;
      const hay = [a.id, a.about.title, a.about.summary_plain, ...a.about.use_when, ...a.about.outputs].join(" ").toLowerCase();
      return words.every((w) => hay.includes(w)) || matchesQuery(f.text, [a.about.title, a.id]);
    })
    .sort((a, b) => rank(a.kind) - rank(b.kind) || a.about.title.localeCompare(b.about.title));
}

/** A plot card's example: the latest catalog version of the recipe. */
function CatalogPlotExample({ id, title }: { id: string; title: string }) {
  const catalog = useCatalog();
  const versions = (catalog.data?.plots ?? []).filter((p) => p.id === id && !p.yanked).map((p) => p.version ?? "1.0.0");
  const version = versions.sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).at(-1);
  if (!version) return null;
  return <PlotExample id={id} version={version} title={title} height={320} className="rounded-lg border p-2" />;
}

/** The model the grid features on an ink block with the what-if playground (the MMM card). */
const FEATURED_IDS = ["mmm.bayesian", "mmm.hierarchical_geo"];

/** The featured item among `items` (the MMM model), or `null` when the catalog has none. */
export function featuredAbout(items: readonly CatalogAbout[]): CatalogAbout | null {
  const models = items.filter((a) => a.kind === "model");
  for (const id of FEATURED_IDS) {
    const hit = models.find((a) => a.id === id);
    if (hit) return hit;
  }
  return models.find((a) => a.id.startsWith("mmm.")) ?? null;
}

const FILTER_PILL =
  "focus-ring inline-flex min-h-10 items-center gap-1.5 rounded-full border border-border-strong bg-card px-4 text-sm font-semibold transition-colors hover:border-foreground aria-pressed:border-foreground aria-pressed:bg-foreground aria-pressed:text-background";

/** The featured card: the one ink block of the page, with the playground beside it. */
function FeaturedCard({ entry, onOpen }: { entry: CatalogAbout; onOpen: () => void }) {
  const a = entry.about;
  return (
    <section
      aria-labelledby="catalog-featured-title"
      data-testid="catalog-featured"
      className="grid gap-6 rounded-3xl bg-block p-6 text-block-foreground sm:p-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] lg:gap-8"
    >
      <div className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="inline-flex items-center rounded-full bg-block-raised px-2.5 py-0.5 text-xs font-semibold">{KIND_META.model.label}</span>
          <span className="inline-flex items-center rounded-full bg-block-raised px-2.5 py-0.5 text-xs font-semibold">Most used</span>
        </div>
        <h3 id="catalog-featured-title" className="display-title text-[clamp(1.75rem,4vw,2.25rem)] leading-[1.05] [overflow-wrap:anywhere]">
          {a.title}
        </h3>
        <p className="max-w-[52ch] text-base leading-relaxed text-block-muted">{a.summary_plain}</p>
        {a.use_when[0] ? (
          <p className="max-w-[52ch] text-sm text-block-muted">
            <span className="font-semibold text-block-foreground">Good for: </span>
            {a.use_when[0]}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <DifficultyChip difficulty={a.difficulty} onBlock />
          <RuntimeChip runtime={a.typical_runtime} onBlock />
          <span className="font-mono text-xs text-block-muted">{entry.id}</span>
        </div>
        <div className="flex flex-wrap gap-2.5 pt-3 [&_[data-slot=button]]:h-auto [&_[data-slot=button]]:min-h-11 [&_[data-slot=button]]:rounded-full [&_[data-slot=button]]:px-5">
          {/* the last two classes: the focus ring of the block (`BLOCK_RING` in features/library/ui.tsx) */}
          <span className="contents [&_[data-slot=button]]:bg-block-foreground [&_[data-slot=button]]:text-block [&_[data-slot=button]:hover]:bg-block-foreground/85 [&_[data-slot=button]]:[--card:var(--block)] [&_[data-slot=button]]:[--ring:var(--block-foreground)]">
            <StartButton kind="model" id={entry.id}>
              Use this model
            </StartButton>
          </span>
          <Button variant="ghost" onClick={onOpen} className="text-block-foreground shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised hover:text-block-foreground">
            Read the card
          </Button>
        </div>
        <EmptyArt kind="models" onBlock className="mt-auto hidden size-[150px] lg:block" />
      </div>
      <div className="min-w-0 self-center">
        <CatalogPlayground />
      </div>
    </section>
  );
}

export function CatalogBrowser({
  selected,
  onSelect,
  query: text = "",
  onQuery,
  kind: urlKind,
  difficulty: urlDifficulty,
  onFilters,
  teamCount = 0,
  onShowTeam,
}: {
  selected: string | null;
  onSelect: (key: string | null) => void;
  /** The text of the search box (the page keeps it in the URL as `?q=`). */
  query?: string;
  onQuery: (query: string) => void;
  /** The kind pill and the difficulty of the URL (`?kind=`, `?difficulty=`); nothing: all. */
  kind?: string | undefined;
  difficulty?: Difficulty | undefined;
  /** Write the two filters to the URL (`undefined` removes a key). Use a history replace. */
  onFilters: (next: { kind: AboutKind | undefined; difficulty: Difficulty | undefined }) => void;
  /** Items in Catalog › Yours; with `onShowTeam`, a "Your team's" filter pill opens that tab. */
  teamCount?: number;
  onShowTeam?: (() => void) | undefined;
}) {
  const index = useAboutIndex();
  // The filters are in the URL like the text: a reload, Back and a copied link keep them.
  const kind: AboutKind | "all" = KIND_ORDER.find((k) => k === urlKind) ?? "all";
  const difficulty: Difficulty | "all" = urlDifficulty ?? "all";
  const setKind = (k: AboutKind | "all") => onFilters({ kind: k === "all" ? undefined : k, difficulty: urlDifficulty });
  const setDifficulty = (d: Difficulty | "all") => onFilters({ kind: kind === "all" ? undefined : kind, difficulty: d === "all" ? undefined : d });
  const all = useMemo(() => filterAbouts(index.data?.items ?? [], { kind, difficulty, text }), [index.data, kind, difficulty, text]);
  const featured = useMemo(() => featuredAbout(index.data?.items ?? []), [index.data]);
  const counts = useMemo(() => {
    const m = new Map<AboutKind, number>();
    for (const a of index.data?.items ?? []) m.set(a.kind, (m.get(a.kind) ?? 0) + 1);
    return m;
  }, [index.data]);
  // The box shows the text of a link (?q=) while the cards load.
  if (index.isPending) {
    return (
      <div className="grid gap-6">
        {text ? <ListToolbar things="catalog items" label="Search the catalog" value={text} onChange={onQuery} /> : null}
        <LoadingRows rows={6} />
      </div>
    );
  }
  if (index.isError) return <ErrorNotice error={index.error} title="The app cannot load the catalog" onRetry={() => void index.refetch()} />;
  const open = parseAboutKey(selected);
  const entry = open ? index.data.byKey.get(aboutKey(open.kind, open.id)) : undefined;
  const kinds = KIND_ORDER.filter((k) => counts.get(k));
  // The featured card stands in for its tile while it shows (plain view: no search, no other kind).
  const showFeatured = featured !== null && !text.trim() && (kind === "all" || kind === "model") && (difficulty === "all" || difficulty === featured.about.difficulty);
  const items = showFeatured ? all.filter((a) => !(a.kind === featured.kind && a.id === featured.id)) : all;
  return (
    <div className="grid gap-6" data-tour="catalog.browse">
      <ListToolbar
        things="catalog items"
        label="Search the catalog"
        placeholder="Type a word, for example forecast, seasonality or holdout"
        value={text}
        onChange={onQuery}
        count={{ shown: all.length, total: index.data.items.length }}
      >
        <NativeSelect aria-label="Difficulty" value={difficulty} onChange={(e) => setDifficulty(e.target.value as Difficulty | "all")} className="w-48 rounded-full">
          <option value="all">Any difficulty</option>
          <option value="beginner">Beginner friendly</option>
          <option value="intermediate">Some experience</option>
          <option value="expert">For experts</option>
        </NativeSelect>
        <Button asChild variant="ghost" className="ml-auto h-auto min-h-10 bg-secondary px-4 text-foreground hover:bg-border-strong hover:text-foreground">
          <Link to="/choose">
            <Sparkles /> Help me choose
          </Link>
        </Button>
        <div role="group" aria-label="Kind" className="flex basis-full flex-wrap gap-2">
          {(["all", ...kinds] as const).map((k) => (
            <button key={k} type="button" aria-pressed={kind === k} onClick={() => setKind(k)} className={FILTER_PILL}>
              {k === "all" ? "Everything" : KIND_META[k].plural}
              <span className="font-medium tabular opacity-65">{k === "all" ? index.data.items.length : counts.get(k)}</span>
            </button>
          ))}
          {onShowTeam && teamCount > 0 ? (
            <button type="button" onClick={onShowTeam} className={cn(FILTER_PILL, "border-highlight-ink/40 bg-highlight-soft text-highlight-ink")}>
              Your team's
              <span className="font-medium tabular opacity-65">{teamCount}</span>
            </button>
          ) : null}
        </div>
      </ListToolbar>
      {showFeatured ? <FeaturedCard entry={featured} onOpen={() => onSelect(aboutKey(featured.kind, featured.id))} /> : null}
      {items.length ? (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Catalog items">
          {items.map((a) => (
            <li key={aboutKey(a.kind, a.id)}>
              <AboutTile
                about={a.about}
                kind={a.kind}
                id={a.id}
                glyph={<CatalogGlyph kind={glyphKind(a.kind)} seed={a.id} size="sm" />}
                onOpen={() => onSelect(aboutKey(a.kind, a.id))}
                selected={selected === aboutKey(a.kind, a.id)}
              />
            </li>
          ))}
        </ul>
      ) : showFeatured ? null : text ? (
        <ListNoMatches things="catalog items" query={text} filtered={kind !== "all" || difficulty !== "all"} onClear={() => onQuery("")} />
      ) : (
        <EmptyState title="No item matches" illustration={<EmptyArt kind="search" />}>
          No catalog item has this kind and this difficulty. Select a different kind or a different difficulty.
        </EmptyState>
      )}
      {entry ? (
        <AboutDialog
          about={entry.about}
          kind={entry.kind}
          id={entry.id}
          open
          onOpenChange={(o) => (o ? undefined : onSelect(null))}
          actions={entry.kind === "plot" ? undefined : <StartButton kind={entry.kind} id={entry.id} />}
          media={entry.kind === "plot" ? <CatalogPlotExample id={entry.id} title={entry.about.title} /> : undefined}
        />
      ) : null}
    </div>
  );
}
