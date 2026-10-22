/**
 * Catalog › Yours (v0.18 §3.4): what your team added.
 *
 * - **From your team**: library items of the project and the tenant (model recipes, prep
 *   presets, chart templates, workflow templates) with who made them, when, and Use / Edit.
 * - **Installed by your team**: catalog entries from a non-built-in distribution (`provider`)
 *   and plugin workflow templates (`source: plugin`).
 * - **Problems** (tenant admins and operators only): the catalog diagnostics.
 *
 * Every title, summary and name here is untrusted user or plugin text: React text only.
 * Warm copy for the empty state and the panel (plan D28); reference text stays STE.
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, Pencil, Play, Plug } from "lucide-react";
import { useMemo } from "react";

import { can } from "@/api/access";
import { EmptyState, ErrorNotice, LoadingRows } from "@/components/common";
import { AddYourOwn } from "@/components/extend/add-your-own";
import { CatalogGlyph, EmptyLibraryArt, glyphKind } from "@/components/illustrations";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/features/auth/workspace";
import { useLibraryItems } from "@/features/library/api";
import { TAB_OF_KIND, type LibrarySearch } from "@/features/library/search";
import { KIND_LABEL, type LibraryItem } from "@/features/library/types";
import { useWorkflowTemplates } from "@/features/workflows/api";
import { cn } from "@/lib/utils";

import { CatalogProblems } from "./diagnostics-list";
import { useInstalledExtensions, type InstalledExtension } from "./api";
import { itemByline, teamItems } from "./team";

const PILL_SM = "h-auto min-h-9 rounded-full px-4 text-xs";

function LibLink({ search, className, children, label }: { search: LibrarySearch; className?: string; children: React.ReactNode; label?: string }) {
  return (
    <Link to="/library" search={search} className={className} aria-label={label}>
      {children}
    </Link>
  );
}

/** How many things Yours lists: library items and installed entries (the tab goes first when > 0). */
export function useTeamCount(): { count: number; isPending: boolean } {
  const library = useLibraryItems({});
  const installed = useInstalledExtensions();
  const count = teamItems(library.data ?? []).length + installed.items.length;
  return { count, isPending: library.isPending };
}

const CHIP = "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap";

function TeamRow({ item, me }: { item: LibraryItem; me: string }) {
  const ws = useWorkspace();
  const canEdit = item.scope === "tenant" ? can(ws.grants, "update", "tenant_library") : can(ws.grants, "update", "library");
  const tab = TAB_OF_KIND[item.kind];
  return (
    <li className="grid grid-cols-[2.75rem_minmax(0,1fr)] items-start gap-x-3.5 gap-y-2 border-b py-4 last:border-b-0 sm:grid-cols-[2.75rem_minmax(0,1fr)_auto] sm:items-center">
      <CatalogGlyph kind={glyphKind(item.kind)} seed={item.id} size="sm" />
      <div className="min-w-0">
        <p className="font-display text-base leading-snug font-bold [overflow-wrap:anywhere]">{item.title}</p>
        <p className="text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">{KIND_LABEL[item.kind]}</span> · {itemByline(item, me)}
        </p>
        {item.summary ? <p className="mt-1 line-clamp-2 text-sm text-muted-foreground [overflow-wrap:anywhere]">{item.summary}</p> : null}
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <span className={cn(CHIP, item.scope === "tenant" ? "bg-highlight-soft text-highlight-ink" : "bg-muted text-muted-foreground")}>{item.scope === "tenant" ? "Whole tenant" : "This project"}</span>
          {item.created_by === "system" ? <span className={cn(CHIP, "bg-attention-soft text-attention")}>Sample</span> : null}
        </div>
      </div>
      <div className="col-span-2 flex flex-wrap gap-2 sm:col-span-1 sm:justify-end">
        {item.kind === "workflow_template" ? (
          <Button size="sm" className={PILL_SM} asChild>
            <Link to="/workflows" search={{ tab: "templates" }} aria-label={`Start a workflow from ${item.title} in the gallery`}>
              <Play /> Start a workflow
            </Link>
          </Button>
        ) : item.kind === "chart_template" ? (
          <Button size="sm" className={PILL_SM} asChild>
            <LibLink search={{ tab, item: item.id }} label={`Use ${item.title}`}>
              Use <ArrowRight />
            </LibLink>
          </Button>
        ) : (
          <Button size="sm" className={PILL_SM} asChild>
            <Link to="/build/new" aria-label={`Use ${item.title} in Build`}>
              Use in Build <ArrowRight />
            </Link>
          </Button>
        )}
        <Button size="sm" variant="ghost" className={cn(PILL_SM, "bg-muted text-foreground hover:bg-surface-hover")} asChild>
          <LibLink search={{ tab, item: item.id }} label={`${canEdit ? "Edit" : "View"} ${item.title}`}>
            {canEdit ? <Pencil /> : <ArrowRight />} {canEdit ? "Edit" : "View"}
          </LibLink>
        </Button>
      </div>
    </li>
  );
}

const EXT_KIND_LABEL: Record<InstalledExtension["kind"] | "workflow_template", string> = {
  model: "Model",
  step: "Prep step",
  plot: "Chart type",
  workflow_template: "Workflow template",
};

interface InstalledRow {
  kind: InstalledExtension["kind"] | "workflow_template";
  id: string;
  version: string;
  title: string;
  provider: string | null;
}

function InstalledList({ rows }: { rows: InstalledRow[] }) {
  return (
    <ul className="rounded-2xl border bg-card px-5" aria-label="Installed by your team">
      {rows.map((r) => (
        <li key={`${r.kind}:${r.id}`} className="grid grid-cols-[2.75rem_minmax(0,1fr)] items-center gap-3.5 border-b py-3.5 last:border-b-0 sm:grid-cols-[2.75rem_minmax(0,1fr)_auto]">
          <CatalogGlyph kind={glyphKind(r.kind)} seed={r.id} size="sm" />
          <div className="min-w-0">
            <p className="font-display text-base leading-snug font-bold [overflow-wrap:anywhere]">{r.title}</p>
            <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
              {r.id}
              {r.version ? `@${r.version}` : ""}
            </p>
          </div>
          <div className="col-span-2 flex flex-wrap gap-1.5 sm:col-span-1 sm:justify-end">
            <span className={cn(CHIP, "bg-muted text-muted-foreground")}>{EXT_KIND_LABEL[r.kind]}</span>
            <span className={cn(CHIP, "bg-accent text-accent-foreground")}>
              <Plug aria-hidden className="size-3" />
              {r.provider ? `Installed from ${r.provider}` : "Installed plugin"}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function SectionHead({ id, title, count, children }: { id: string; title: string; count?: number; children?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="grid gap-1">
        <h3 id={id} className="text-xl leading-tight font-bold">
          {title}
          {count !== undefined ? <span className="ml-2 align-middle text-xs font-semibold text-muted-foreground">{" "}{count}</span> : null}
        </h3>
        {children ? <p className="max-w-[62ch] text-sm text-muted-foreground">{children}</p> : null}
      </div>
    </div>
  );
}

/** The Yours tab. */
export function YoursTab() {
  const ws = useWorkspace();
  const library = useLibraryItems({});
  const installed = useInstalledExtensions();
  const templates = useWorkflowTemplates();
  const admin = ws.operator || ws.grants.tenantRole === "admin";
  const items = useMemo(() => teamItems(library.data ?? []), [library.data]);
  const installedRows = useMemo<InstalledRow[]>(
    () => [
      ...installed.items.map((x) => ({ kind: x.kind, id: x.id, version: x.version, title: x.title, provider: x.provider })),
      ...(templates.data?.items ?? [])
        .filter((t) => t.source === "plugin")
        .map((t) => ({ kind: "workflow_template" as const, id: t.id, version: t.version ?? "", title: t.about.title, provider: null })),
    ],
    [installed.items, templates.data],
  );
  const nothing = !library.isPending && !items.length && !installedRows.length;
  return (
    <div className="grid gap-10" data-testid="catalog-yours">
      {nothing ? (
        <EmptyState
          title="Nothing from your team yet"
          illustration={<EmptyLibraryArt />}
          primaryAction={
            <Button className="h-auto min-h-11 rounded-full px-5" asChild>
              <LibLink search={{ tab: "models", new: "model" }}>
                Make your first model recipe <ArrowRight />
              </LibLink>
            </Button>
          }
        >
          This is where your team's own models, prep presets, chart styles and workflow templates show up. Make one in a few minutes, no code needed, and everyone in your project can use it.
        </EmptyState>
      ) : (
        <section aria-labelledby="yours-team" className="grid gap-3">
          <SectionHead id="yours-team" title="From your team" count={items.length}>
            Made with Make your own, for this project or the whole tenant. They never rank among the built-ins in Help me choose.
          </SectionHead>
          {library.isPending ? (
            <LoadingRows rows={3} />
          ) : library.isError ? (
            <ErrorNotice error={library.error} title="The app cannot load your team's items" onRetry={() => void library.refetch()} />
          ) : items.length ? (
            <ul className="rounded-2xl border bg-card px-5" aria-label="From your team">
              {items.map((item) => (
                <TeamRow key={item.id} item={item} me={ws.user.id} />
              ))}
            </ul>
          ) : (
            <p className="rounded-2xl border border-dashed border-border-strong p-5 text-sm text-muted-foreground">
              Nothing made here yet.{" "}
              <LibLink search={{}} className="font-semibold text-primary hover:underline">
                Open Make your own
              </LibLink>{" "}
              to start one.
            </p>
          )}
        </section>
      )}
      {installedRows.length ? (
        <section aria-labelledby="yours-installed" className="grid gap-3">
          <SectionHead id="yours-installed" title="Installed by your team" count={installedRows.length}>
            Plugins your operators installed on this stack. They work like the built-in items.
          </SectionHead>
          <InstalledList rows={installedRows} />
        </section>
      ) : null}
      {admin ? <CatalogProblems operator={ws.operator} /> : null}
      <AddYourOwn kind="any" />
    </div>
  );
}
