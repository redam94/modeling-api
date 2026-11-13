/**
 * The items of one kind in Make your own: cards with the kind's glyph, the title and summary
 * (untrusted text, rendered as text), who made it and when, its scope, and the actions (open,
 * start from it for workflow templates, archive, share with the whole tenant for tenant admins).
 */
import { Archive, ArchiveRestore, ArrowRight, Pencil, Play, Share2 } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { ErrorNotice, LoadingRows, Spinner } from "@/components/common";
import { ListNoMatches, ListToolbar } from "@/components/list-toolbar";
import { Button } from "@/components/ui/button";
import { CatalogGlyph, glyphKind } from "@/components/illustrations";
import { useGrants } from "@/features/auth/workspace";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { useStartFromTemplate } from "./actions";
import { useArchiveLibraryItem, useLibraryItems, usePromoteLibraryItem } from "./api";
import { Chip, PILL_SM, QUIET, ToggleChip } from "./ui";
import type { LibraryItem, LibraryKind } from "./types";

function byline(item: LibraryItem, me: string): string {
  const who = item.created_by === "system" ? "Added with the sample project" : item.created_by === me ? "Made by you" : "Made by your team";
  return `${who} · updated ${relativeTime(item.updated_at)}`;
}

function ItemCard({ item, onOpen, me }: { item: LibraryItem; onOpen: (item: LibraryItem) => void; me: string }) {
  const grants = useGrants();
  const archive = useArchiveLibraryItem();
  const promote = usePromoteLibraryItem();
  const start = useStartFromTemplate();
  const canEdit = item.scope === "tenant" ? can(grants, "update", "tenant_library") : can(grants, "update", "library");
  const canPromote = item.scope === "project" && !item.archived && can(grants, "create", "tenant_library");
  const error = archive.error ?? promote.error ?? start.error;
  return (
    <li className={cn("grid content-start gap-3 rounded-2xl border bg-card p-5", item.archived && "opacity-75")}>
      <div className="flex items-start gap-3">
        <CatalogGlyph kind={glyphKind(item.kind)} seed={item.id} size="sm" />
        <div className="min-w-0 flex-1">
          <h3 className="text-lg leading-tight font-semibold [overflow-wrap:anywhere]">{item.title}</h3>
          <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{item.name}</p>
        </div>
      </div>
      {item.summary ? <p className="text-muted-foreground [overflow-wrap:anywhere]">{item.summary}</p> : null}
      <div className="flex flex-wrap gap-1.5">
        <Chip tone={item.scope === "tenant" ? "highlight" : "plain"}>{item.scope === "tenant" ? "Whole tenant" : "This project"}</Chip>
        <Chip>v{item.latest_revision}</Chip>
        {item.created_by === "system" ? <Chip tone="attention">Sample</Chip> : null}
        {item.archived ? <Chip tone="warn">Archived</Chip> : null}
        {(item.tags ?? []).slice(0, 4).map((t) => (
          <Chip key={t}>{t}</Chip>
        ))}
      </div>
      <p className="text-xs text-hint">{byline(item, me)}</p>
      <div className="mt-auto flex flex-wrap gap-2 border-t border-dashed border-border-strong pt-3">
        {item.kind === "workflow_template" ? (
          <Button size="sm" className={PILL_SM} disabled={item.archived || start.busy !== null} onClick={() => void start.start(item)}>
            {start.busy === item.id ? <Spinner /> : <Play />} Start from this
          </Button>
        ) : null}
        <Button size="sm" variant={item.kind === "workflow_template" ? "ghost" : "default"} className={cn(PILL_SM, item.kind === "workflow_template" && QUIET)} onClick={() => onOpen(item)} aria-label={`${canEdit ? "Open" : "View"} ${item.title}`}>
          {canEdit ? <Pencil /> : <ArrowRight />} {canEdit ? "Open" : "View"}
        </Button>
        {canEdit ? (
          <Button size="sm" variant="ghost" className={cn(PILL_SM, QUIET)} disabled={archive.isPending} onClick={() => archive.mutate({ id: item.id, archived: !item.archived })} aria-label={`${item.archived ? "Show again" : "Archive"} ${item.title}`}>
            {item.archived ? <ArchiveRestore /> : <Archive />} {item.archived ? "Show again" : "Archive"}
          </Button>
        ) : null}
        {canPromote ? (
          <Button size="sm" variant="ghost" className={cn(PILL_SM, QUIET)} disabled={promote.isPending} onClick={() => promote.mutate(item.id)} aria-label={`Share ${item.title} with the whole tenant`}>
            <Share2 /> Share with the tenant
          </Button>
        ) : null}
      </div>
      {error ? <ErrorNotice error={error} title="The change was not made" /> : null}
    </li>
  );
}

/** The things of one kind, as the search box names them. */
const THINGS: Record<LibraryKind, string> = { model_recipe: "model recipes", prep_preset: "prep presets", chart_template: "chart templates", workflow_template: "workflow templates" };

export function LibraryList({
  kind,
  onOpen,
  empty,
  me,
  query,
  onQuery,
}: {
  kind: LibraryKind;
  onOpen: (item: LibraryItem) => void;
  empty: React.ReactNode;
  me: string;
  /** The text of the search box (the page keeps it in the URL as `?q=`); the server filters by it. */
  query: string;
  onQuery: (query: string) => void;
}) {
  const [archived, setArchived] = useState(false);
  const items = useLibraryItems({ kind, q: query || undefined, include_archived: archived || undefined });
  const list = items.data ?? [];
  const things = THINGS[kind];
  return (
    <div className="grid gap-4">
      <ListToolbar things={things} placeholder="Search by name or title" value={query} onChange={onQuery} count={items.data && !items.isPlaceholderData ? { shown: list.length } : undefined}>
        <ToggleChip pressed={archived} onClick={() => setArchived(!archived)}>
          Show archived
        </ToggleChip>
      </ListToolbar>
      {items.isPending ? (
        <LoadingRows rows={3} />
      ) : items.isError ? (
        <ErrorNotice error={items.error} title="The app could not load your items" onRetry={() => void items.refetch()} />
      ) : list.length ? (
        <ul data-testid="library-list" className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map((item) => (
            <ItemCard key={item.id} item={item} onOpen={onOpen} me={me} />
          ))}
        </ul>
      ) : query ? (
        <ListNoMatches things={things} query={query} onClear={() => onQuery("")} />
      ) : archived ? (
        <p className="rounded-2xl border border-dashed border-border-strong p-6 text-muted-foreground">There are no {things}, and there are none in the archive.</p>
      ) : (
        empty
      )}
    </div>
  );
}
