/**
 * The header of the compare page (v0.14): a chip per run in its chart colour, with a link to
 * the run page and a button that takes the run out of the comparison, and "Add run" (a dialog
 * that finds runs of the project by name). Run names are untrusted text.
 */
import { Link } from "@tanstack/react-router";
import { Plus, X } from "lucide-react";
import { useState } from "react";

import { MAX_COMPARE_RUNS } from "@/api/experiments";
import { useRuns } from "@/api/queries";
import { runName } from "@/api/tracking";
import type { Run } from "@/api/types";
import { ErrorNotice, LoadingRows, Mono, StateBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { Input } from "@/components/ui/primitives";
import { useDebounced } from "@/hooks/use-debounced";
import { relativeTime, shortId } from "@/lib/format";

import { runColor } from "./compare-model";

export function RunSwatch({ index }: { index: number }) {
  return <span aria-hidden className="inline-block size-2.5 shrink-0 rounded-full" style={{ background: runColor(index) }} />;
}

export function RunChips({ runs, labels, onRemove, onAdd }: { runs: readonly Run[]; labels: readonly string[]; onRemove: (id: string) => void; onAdd: (id: string) => void }) {
  const [adding, setAdding] = useState(false);
  return (
    <div className="flex flex-wrap items-center gap-2" role="list" aria-label="Compared runs">
      {runs.map((r, i) => (
        <span key={r.id} role="listitem" className="inline-flex max-w-full items-center gap-2 rounded-full border bg-card py-1 pr-1 pl-3 text-[13px]">
          <RunSwatch index={i} />
          <Link to="/runs/$runId" params={{ runId: r.id }} className="max-w-64 truncate hover:underline" title={r.id}>
            {labels[i] ?? shortId(r.id)}
          </Link>
          <StateBadge state={r.state} />
          <button
            type="button"
            className="rounded-full p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-40"
            aria-label={`Remove ${labels[i] ?? r.id} from the comparison`}
            disabled={runs.length <= 2}
            title={runs.length <= 2 ? "A comparison needs two runs" : undefined}
            onClick={() => onRemove(r.id)}
          >
            <X className="size-3.5" />
          </button>
        </span>
      ))}
      <Button variant="outline" size="sm" disabled={runs.length >= MAX_COMPARE_RUNS} onClick={() => setAdding(true)}>
        <Plus /> Add run
      </Button>
      <Dialog open={adding} onOpenChange={setAdding}>
        <DialogContent>
          <DialogTitle>Add a run to the comparison</DialogTitle>
          <DialogDescription>Find a run by its name. You can compare {MAX_COMPARE_RUNS} runs maximum.</DialogDescription>
          <AddRunList
            exclude={runs.map((r) => r.id)}
            onPick={(id) => {
              setAdding(false);
              onAdd(id);
            }}
          />
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AddRunList({ exclude, onPick }: { exclude: readonly string[]; onPick: (id: string) => void }) {
  const [name, setName] = useState("");
  const q = useDebounced(name.trim(), 300);
  const runs = useRuns({ limit: 50, ...(q ? { name: q } : {}) }, { live: false });
  const items = (runs.data?.items ?? []).filter((r) => !exclude.includes(r.id));
  return (
    <div className="grid gap-3">
      <Input aria-label="Find runs by name" placeholder="Find runs by name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      {runs.isError ? (
        <ErrorNotice error={runs.error} title="The app could not load the runs" />
      ) : runs.isPending ? (
        <LoadingRows rows={3} />
      ) : items.length ? (
        <ul className="grid max-h-80 gap-1 overflow-auto">
          {items.map((r) => (
            <li key={r.id}>
              <button type="button" className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-accent" onClick={() => onPick(r.id)}>
                <StateBadge state={r.state} />
                <span className="min-w-0 flex-1 truncate">{runName(r) ?? <Mono>{shortId(r.id)}</Mono>}</span>
                <span className="text-xs text-muted-foreground">{r.kind}</span>
                <span className="text-xs text-muted-foreground">{relativeTime(r.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[13px] text-muted-foreground">No runs match.</p>
      )}
    </div>
  );
}
