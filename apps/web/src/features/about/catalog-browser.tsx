/**
 * Every item a user can pick, by its About card: models, preparation steps, splits, Guided
 * analyses, workflow templates, workflow nodes and governance checks
 * (`GET /v1/catalog/abouts`), filterable by kind, difficulty and text.
 */
import { Search, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";

import { EmptyState, ErrorNotice, LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { AboutDialog, AboutTile } from "./about-card";
import { useAboutIndex } from "./api";
import { StartButton } from "./chooser";
import { aboutKey, KIND_META, KIND_ORDER, parseAboutKey } from "./labels";
import type { AboutKind, CatalogAbout, Difficulty } from "./types";

/** Items matching a text (title, id, summary, use-when) and filters, in catalog order. */
export function filterAbouts(items: readonly CatalogAbout[], f: { kind: AboutKind | "all"; difficulty: Difficulty | "all"; text: string }): CatalogAbout[] {
  const words = f.text.toLowerCase().split(/\s+/).filter(Boolean);
  const rank = (k: AboutKind) => (KIND_ORDER.includes(k) ? KIND_ORDER.indexOf(k) : KIND_ORDER.length);
  return items
    .filter((a) => (f.kind === "all" || a.kind === f.kind) && (f.difficulty === "all" || a.about.difficulty === f.difficulty))
    .filter((a) => {
      if (!words.length) return true;
      const hay = [a.id, a.about.title, a.about.summary_plain, ...a.about.use_when, ...a.about.outputs].join(" ").toLowerCase();
      return words.every((w) => hay.includes(w));
    })
    .sort((a, b) => rank(a.kind) - rank(b.kind) || a.about.title.localeCompare(b.about.title));
}

export function CatalogBrowser({ selected, onSelect }: { selected: string | null; onSelect: (key: string | null) => void }) {
  const index = useAboutIndex();
  const [kind, setKind] = useState<AboutKind | "all">("all");
  const [difficulty, setDifficulty] = useState<Difficulty | "all">("all");
  const [text, setText] = useState("");
  const items = useMemo(() => filterAbouts(index.data?.items ?? [], { kind, difficulty, text }), [index.data, kind, difficulty, text]);
  const counts = useMemo(() => {
    const m = new Map<AboutKind, number>();
    for (const a of index.data?.items ?? []) m.set(a.kind, (m.get(a.kind) ?? 0) + 1);
    return m;
  }, [index.data]);
  if (index.isPending) return <LoadingRows rows={6} />;
  if (index.isError) return <ErrorNotice error={index.error} title="The app cannot load the catalog" onRetry={() => void index.refetch()} />;
  const open = parseAboutKey(selected);
  const entry = open ? index.data.byKey.get(aboutKey(open.kind, open.id)) : undefined;
  const kinds = KIND_ORDER.filter((k) => counts.get(k));
  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card p-4">
        <div className="grid gap-0.5">
          <p className="font-semibold tracking-tight">Not sure where to start?</p>
          <p className="text-[13px] text-muted-foreground">Answer some questions about your goal and your data. The app then shows suggestions in order of fit.</p>
        </div>
        <Button asChild>
          <Link to="/choose">
            <Sparkles /> Help me choose
          </Link>
        </Button>
      </div>
      <div className="grid gap-3">
        <div role="group" aria-label="Kind" className="flex flex-wrap gap-1.5">
          {(["all", ...kinds] as const).map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={kind === k}
              onClick={() => setKind(k)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring",
                kind === k && "border-primary/60 bg-accent text-accent-foreground",
              )}
            >
              {k === "all" ? "Everything" : KIND_META[k].plural}
              <span className="text-muted-foreground tabular">{k === "all" ? index.data.items.length : counts.get(k)}</span>
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <div className="relative min-w-[14rem] flex-1 sm:max-w-sm">
            <Search aria-hidden className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
            <Input aria-label="Search the catalog" placeholder="Type a word, for example forecast, seasonality or holdout" value={text} onChange={(e) => setText(e.target.value)} className="pl-8" />
          </div>
          <NativeSelect aria-label="Difficulty" value={difficulty} onChange={(e) => setDifficulty(e.target.value as Difficulty | "all")} className="w-48">
            <option value="all">Any difficulty</option>
            <option value="beginner">Beginner friendly</option>
            <option value="intermediate">Some experience</option>
            <option value="expert">For experts</option>
          </NativeSelect>
        </div>
      </div>
      {items.length ? (
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="Catalog items">
          {items.map((a) => (
            <li key={aboutKey(a.kind, a.id)}>
              <AboutTile about={a.about} kind={a.kind} id={a.id} onOpen={() => onSelect(aboutKey(a.kind, a.id))} selected={selected === aboutKey(a.kind, a.id)} />
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState icon={<Search />} title="No item matches">
          Type a different word, or clear the filters.
        </EmptyState>
      )}
      {entry ? (
        <AboutDialog
          about={entry.about}
          kind={entry.kind}
          id={entry.id}
          open
          onOpenChange={(o) => (o ? undefined : onSelect(null))}
          actions={<StartButton kind={entry.kind} id={entry.id} />}
        />
      ) : null}
    </div>
  );
}
