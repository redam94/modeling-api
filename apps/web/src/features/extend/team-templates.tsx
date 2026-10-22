/**
 * The workflow gallery's "From your team" section (v0.18 D8): team workflow templates from the
 * library (project and tenant). "Start from this" asks the server for the template's document
 * (`:use`) and creates a workflow from it (`POST /v1/workflows {document}`), then opens it.
 *
 * Titles and summaries are untrusted user text (React text only).
 */
import { ArrowRight, Play } from "lucide-react";

import { ErrorNotice, Spinner } from "@/components/common";
import { CatalogGlyph } from "@/components/illustrations";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/features/auth/workspace";
import { useStartFromTemplate } from "@/features/library/actions";
import { useLibraryPicker } from "@/features/library/api";
import type { LibraryItem } from "@/features/library/types";
import { LibraryLink } from "@/features/library/ui";

import { itemByline, teamItems } from "./team";

const PILL_SM = "h-auto min-h-9 rounded-full px-4 text-xs";

/** The team's workflow templates (non-archived), for the gallery. */
export function useTeamTemplates(): { items: LibraryItem[]; isPending: boolean; error: unknown } {
  const q = useLibraryPicker("workflow_template");
  return { items: teamItems(q.data ?? []), isPending: q.isPending, error: q.error };
}

export function TeamTemplateCards({ items, mayCreate }: { items: readonly LibraryItem[]; mayCreate: boolean }) {
  const ws = useWorkspace();
  const start = useStartFromTemplate();
  return (
    <div className="grid gap-3">
      <ul className="grid gap-4 lg:grid-cols-2" aria-label="Workflow templates from your team" data-testid="team-templates">
        {items.map((item) => (
          <li key={item.id} className="grid content-start gap-3 rounded-2xl border bg-card p-5">
            <div className="flex items-start gap-3">
              <CatalogGlyph kind="workflow" seed={item.id} size="sm" />
              <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold text-muted-foreground">From your team</p>
                <h3 className="text-xl leading-tight font-bold [overflow-wrap:anywhere]">{item.title}</h3>
              </div>
            </div>
            {item.summary ? <p className="text-muted-foreground [overflow-wrap:anywhere]">{item.summary}</p> : null}
            <p className="text-xs text-hint">
              {item.scope === "tenant" ? "Shared with the whole tenant" : "Shared with this project"} · {itemByline(item, ws.user.id)}
            </p>
            <div className="flex flex-wrap gap-2 border-t border-dashed border-border-strong pt-3">
              {mayCreate ? (
                <Button size="sm" className={PILL_SM} disabled={start.busy !== null} onClick={() => void start.start(item)} aria-label={`Start from ${item.title}`}>
                  {start.busy === item.id ? <Spinner /> : <Play />} Start from this
                </Button>
              ) : null}
              <Button size="sm" variant="ghost" className={`${PILL_SM} bg-muted text-foreground hover:bg-surface-hover`} asChild>
                <LibraryLink search={{ tab: "workflows", item: item.id }}>
                  See the template <ArrowRight />
                </LibraryLink>
              </Button>
            </div>
          </li>
        ))}
      </ul>
      {start.error ? <ErrorNotice error={start.error} title="The workflow was not created" /> : null}
    </div>
  );
}
