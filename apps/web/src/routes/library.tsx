/**
 * Make your own (`/library`, v0.18 D1-D9, §3.4): the team's own models, prep presets, chart
 * templates and workflow templates, made without code.
 *
 * - The overview has one tab per kind (Models · Prep · Charts · Workflow templates): an ink
 *   block that starts a new one (the page's one block, with its one inverted ink call to action) and
 *   the items of that kind.
 * - `?new=model|preset|prep|chart` opens a builder for a new item (`source=` starts a chart
 *   template from a chart type, `base=` a preset from a catalog model); `?item=lib_…` opens a
 *   saved item in its builder, where a save makes a new revision.
 *
 * The search params are validated by `features/library/search.ts` (the router registers this
 * page lazily: its chunk is `library-*`, signed-in only, never in share or client bundles).
 */
import { useNavigate, useSearch } from "@tanstack/react-router";
import { BookOpen, ChartSpline, Layers, Workflow } from "lucide-react";
import { useRef, type ReactNode } from "react";

import { EmptyArt, type EmptyArtKind } from "@/components/brand/empty-art";
import { EmptyState, ErrorNotice, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { useWorkspace } from "@/features/auth/workspace";
import type { Saved } from "@/features/library/actions";
import { useLibraryItem, useLibraryItems, useLibraryRevision } from "@/features/library/api";
import { ChartBuilder } from "@/features/library/chart-builder";
import { newChartTemplate, newModelRecipe, newPrepPreset, newPresetRecipe } from "@/features/library/doc";
import { LibraryList } from "@/features/library/library-list";
import { ModelBuilder } from "@/features/library/model-builder";
import { PrepBuilder } from "@/features/library/prep-builder";
import { LIBRARY_TABS, TAB_OF_KIND, validateLibrarySearch, type LibraryNew, type LibrarySearch, type LibraryTab } from "@/features/library/search";
import { BLOCK_CTA, PILL } from "@/features/library/ui";
import { WorkflowTemplateEditor } from "@/features/library/workflow-template-editor";
import { docOfKind, type LibraryDoc, type LibraryItem, type LibraryKind } from "@/features/library/types";
import { cn } from "@/lib/utils";

const TAB_LABEL: Record<LibraryTab, string> = { models: "Models", prep: "Prep", charts: "Charts", workflows: "Workflow templates" };
const TAB_KIND: Record<LibraryTab, LibraryKind> = { models: "model_recipe", prep: "prep_preset", charts: "chart_template", workflows: "workflow_template" };

function useLibraryNav() {
  const navigate = useNavigate();
  return (search: LibrarySearch, replace = false) => void navigate({ to: "/library", search: search as never, replace });
}

// ------------------------------------------------------------------------------ overview

interface Lead {
  title: string;
  body: string;
  cta: string;
  anchor?: string;
  onCta: () => void;
  second?: { label: string; onClick: () => void };
}

/** The glyph beside each kind's lead. */
const LEAD_ART: Record<LibraryTab, EmptyArtKind> = { models: "models", prep: "datasets", charts: "dashboards", workflows: "workflows" };

function LeadBlock({ lead, art }: { lead: Lead; art: ReactNode }) {
  return (
    <section className="grid items-center gap-6 overflow-hidden rounded-3xl bg-block p-[clamp(1.5rem,4vw,2.75rem)] text-block-foreground md:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
      <div className="grid justify-items-start gap-4">
        <h2 className="display-title text-[clamp(1.75rem,4vw,2.25rem)] leading-[1.05] text-balance">{lead.title}</h2>
        <p className="max-w-[48ch] text-lg leading-relaxed text-block-muted">{lead.body}</p>
        <div className="flex flex-wrap gap-3 pt-2">
          <Button className={cn(PILL, BLOCK_CTA)} onClick={lead.onCta} data-tour={lead.anchor}>
            {lead.cta}
          </Button>
          {lead.second ? (
            <Button variant="ghost" className={cn(PILL, "text-block-foreground shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised hover:text-block-foreground")} onClick={lead.second.onClick}>
              {lead.second.label}
            </Button>
          ) : null}
        </div>
      </div>
      <div className="mx-auto w-full max-w-[220px] max-md:max-w-[140px]">{art}</div>
    </section>
  );
}

function EmptyKind({ tab, onNew }: { tab: LibraryTab; onNew: () => void }) {
  const copy: Record<LibraryTab, { title: string; body: string; cta?: string }> = {
    models: { title: "Your first model recipe starts here", body: "Describe a model once, in plain words, and your whole team can fit it on their own data from Build.", cta: "Describe a model" },
    prep: { title: "No prep presets yet", body: "Save the steps you do every time (weekly totals, filling gaps, a log) and add them to any spec in one click.", cta: "Make a prep preset" },
    charts: { title: "No chart templates yet", body: "Restyle a chart once and it shows on every run and model page, on the real data.", cta: "Make a chart template" },
    workflows: { title: "No team workflow templates yet", body: "Open a workflow that works well and choose Save as team template. It shows up here and in the workflow gallery." },
  };
  const c = copy[tab];
  return (
    <EmptyState
      title={c.title}
      illustration={<EmptyArt kind="library" />}
      primaryAction={
        c.cta ? (
          <Button variant="secondary" className={PILL} onClick={onNew}>
            {c.cta}
          </Button>
        ) : undefined
      }
    >
      {c.body}
    </EmptyState>
  );
}

function Overview({ tab: urlTab, query, archived }: { tab: LibraryTab | undefined; query: string; archived: boolean }) {
  const go = useLibraryNav();
  const navigate = useNavigate();
  const ws = useWorkspace();
  // With a search, the numbers on the tabs count what the search found.
  const counts = useLibraryItems(query ? { q: query } : {});
  const count = (t: LibraryTab) => (counts.data ?? []).filter((i) => i.kind === TAB_KIND[t]).length;
  // A search without a tab (the global search's "See all in Make your own") opens the first
  // tab that has a match.
  const tab = urlTab ?? (query ? LIBRARY_TABS.find((t) => count(t) > 0) : undefined) ?? "models";
  // The text and the "Show archived" chip stay in the URL together (a history replace).
  const filters = (q: string, shown: boolean): LibrarySearch => ({ q: q || undefined, archived: shown || undefined });
  const setQuery = (next: string) => go({ tab, ...filters(next, archived) }, true);
  const leads: Record<LibraryTab, Lead> = {
    models: {
      title: "Describe a model in plain words",
      body: "Pick what you want to explain, add the things that drive it and say how sure you are. We turn it into a model your team can fit.",
      cta: "Describe a model",
      anchor: "library.new-model",
      onCta: () => go({ tab: "models", new: "model" }),
      second: { label: "Preset a catalog model", onClick: () => go({ tab: "models", new: "preset" }) },
    },
    prep: { title: "Save the steps you always do", body: "Weekly totals, gaps filled, a log of the skewed columns: bundle them once, reuse them on any dataset.", cta: "Make a prep preset", onCta: () => go({ tab: "prep", new: "prep" }) },
    charts: { title: "Make a chart your own", body: "Start from any chart in the catalog, change what goes where, and it shows on every run, on the real data.", cta: "Make a chart template", onCta: () => go({ tab: "charts", new: "chart" }) },
    workflows: { title: "Turn a working workflow into a team starting point", body: "In any workflow, choose Save as team template. Everyone in your tenant can then start from it here or in the gallery.", cta: "Open workflows", onCta: () => void navigate({ to: "/workflows" }) },
  };
  const icons: Record<LibraryTab, ReactNode> = { models: <Layers />, prep: <BookOpen />, charts: <ChartSpline />, workflows: <Workflow /> };
  return (
    <div className="mx-auto grid w-full max-w-[1180px] grid-cols-[minmax(0,1fr)] gap-8">
      <div className="-mb-6">
        <PageHeader
          eyebrow="Make your own"
          title="Make it yours."
          description="Save the models, prep steps, charts and workflows your team uses again and again. No code needed: everything you make here shows up in Build, the Catalog and on your runs."
        />
      </div>
      <Tabs value={tab} onValueChange={(t) => go({ tab: t as LibraryTab, ...filters(query, archived) }, true)}>
        <TabsList className="h-auto flex-wrap gap-2" aria-label="Kinds">
          {LIBRARY_TABS.map((t) => (
            <TabsTrigger
              key={t}
              value={t}
              className="h-auto min-h-10 gap-2 border border-border-strong bg-card px-4 font-semibold text-foreground hover:border-foreground data-[state=active]:border-foreground data-[state=active]:bg-foreground data-[state=active]:text-background"
            >
              {icons[t]}
              {TAB_LABEL[t]}
              {counts.data ? <span className="tabular font-medium opacity-70">{count(t)}</span> : null}
            </TabsTrigger>
          ))}
        </TabsList>
        {LIBRARY_TABS.map((t) => (
          <TabsContent key={t} value={t} className="grid gap-8 pt-6">
            <LeadBlock lead={leads[t]} art={<EmptyArt kind={LEAD_ART[t]} onBlock className="size-full" />} />
            <section aria-label={`Your ${TAB_LABEL[t].toLowerCase()}`} className="grid gap-4">
              <h2 className="text-2xl font-semibold">From your team</h2>
              <LibraryList
                kind={TAB_KIND[t]}
                me={ws.user.id}
                query={query}
                onQuery={setQuery}
                archived={archived}
                onArchived={(shown) => go({ tab, ...filters(query, shown) }, true)}
                onOpen={(item) => go({ tab: TAB_OF_KIND[item.kind], item: item.id })}
                empty={<EmptyKind tab={t} onNew={leads[t].onCta} />}
              />
            </section>
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

// ------------------------------------------------------------------------------ editors

const NEW_TITLE: Record<LibraryNew, string> = { model: "Describe a model", preset: "Preset a catalog model", prep: "Make a prep preset", chart: "Make a chart template" };

function EditorFrame({ title, tab, children, note }: { title: string; tab: LibraryTab; note: string; children: ReactNode }) {
  return (
    <div className="mx-auto grid w-full max-w-[1280px] grid-cols-[minmax(0,1fr)]">
      <PageHeader
        crumbs={[{ label: "Make your own", to: "/library" }, { label: TAB_LABEL[tab], to: "/library", search: { tab } }, { label: title }]}
        title={<span className="[overflow-wrap:anywhere]">{title}</span>}
        meta={<span>{note}</span>}
      />
      {children}
    </div>
  );
}

function Builder({ kind, itemId, initial, onCreated }: { kind: LibraryKind; itemId: string | null; initial: LibraryDoc; onCreated: (saved: Saved) => void }) {
  if (kind === "model_recipe") {
    const doc = docOfKind("model_recipe", initial);
    return doc ? <ModelBuilder itemId={itemId} initial={doc} onCreated={onCreated} /> : null;
  }
  if (kind === "prep_preset") {
    const doc = docOfKind("prep_preset", initial);
    return doc ? <PrepBuilder itemId={itemId} initial={doc} onCreated={onCreated} /> : null;
  }
  if (kind === "chart_template") {
    const doc = docOfKind("chart_template", initial);
    return doc ? <ChartBuilder itemId={itemId} initial={doc} onCreated={onCreated} /> : null;
  }
  const doc = docOfKind("workflow_template", initial);
  return doc && itemId ? <WorkflowTemplateEditor itemId={itemId} initial={doc} /> : null;
}

function newDoc(kind: LibraryNew, search: LibrarySearch): LibraryDoc {
  if (kind === "model") return newModelRecipe();
  if (kind === "preset") return newPresetRecipe(search.base ? `${search.base}@^1` : "");
  if (kind === "prep") return newPrepPreset();
  return newChartTemplate(search.source ? `${search.source}@^1` : "", {});
}

const KIND_OF_NEW: Record<LibraryNew, LibraryKind> = { model: "model_recipe", preset: "model_recipe", prep: "prep_preset", chart: "chart_template" };

/** A saved item: its latest revision in the builder of its kind. */
function ItemEditor({ id, onCreated }: { id: string; onCreated: (saved: Saved) => void }) {
  const item = useLibraryItem(id);
  const rev = useLibraryRevision(id, item.data?.latest_revision);
  if (item.isError) return <ErrorNotice error={item.error} title="The app could not open this item" />;
  if (rev.isError) return <ErrorNotice error={rev.error} title="The app could not load this item's latest version" />;
  if (!item.data || !rev.data) return <LoadingRows rows={8} />;
  const it: LibraryItem = item.data;
  const doc = docOfKind(it.kind, rev.data.document);
  return (
    <EditorFrame title={it.title} tab={TAB_OF_KIND[it.kind]} note={`Version ${it.latest_revision}`}>
      {doc ? (
        <Builder kind={it.kind} itemId={it.id} initial={doc} onCreated={onCreated} />
      ) : (
        <ErrorNotice error={new Error("The saved document does not have the shape the builder needs. Open the YAML of a new revision to correct it.")} title="The builder cannot open this item" />
      )}
    </EditorFrame>
  );
}

export function LibraryPage() {
  const search = validateLibrarySearch(useSearch({ strict: false }));
  const go = useLibraryNav();
  /** Items made in this visit: their builder stays mounted when the URL gets their id. */
  const created = useRef(new Map<string, { kind: LibraryKind; title: string; tab: LibraryTab; initial: LibraryDoc; key: string }>());
  const fresh = useRef<{ key: string; initial: LibraryDoc } | null>(null);
  // Only for the new → item URL swap right after saving: once the page shows anything else,
  // the entry goes, so opening the item again loads its latest revision (not the blank start).
  for (const id of created.current.keys()) if (id !== search.item) created.current.delete(id);

  if (search.item && !created.current.has(search.item)) return <ItemEditor key={search.item} id={search.item} onCreated={() => undefined} />;

  const made = search.item ? created.current.get(search.item) : undefined;
  if (made) {
    return (
      <EditorFrame title={made.title} tab={made.tab} note="Saved">
        <Builder key={made.key} kind={made.kind} itemId={search.item ?? null} initial={made.initial} onCreated={() => undefined} />
      </EditorFrame>
    );
  }

  if (search.new) {
    const kind = KIND_OF_NEW[search.new];
    const key = `new-${search.new}-${search.source ?? ""}-${search.base ?? ""}`;
    if (fresh.current?.key !== key) fresh.current = { key, initial: newDoc(search.new, search) };
    const initial = fresh.current.initial;
    const tab = TAB_OF_KIND[kind];
    return (
      <EditorFrame title={NEW_TITLE[search.new]} tab={tab} note="Not saved">
        <Builder
          key={key}
          kind={kind}
          itemId={null}
          initial={initial}
          onCreated={(saved) => {
            created.current.set(saved.id, { kind, title: NEW_TITLE[search.new ?? "model"], tab, initial, key });
            go({ tab, item: saved.id }, true);
          }}
        />
      </EditorFrame>
    );
  }

  return <Overview tab={search.tab} query={search.q ?? ""} archived={Boolean(search.archived)} />;
}
