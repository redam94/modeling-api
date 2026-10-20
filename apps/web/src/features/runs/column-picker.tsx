/**
 * The column picker of the runs table (v0.14): the tracked keys of the project's runs (`GET
 * /v1/run-keys`: metric series with their labels, params, tags) as searchable lists. A metric
 * column shows one summary of one series (last, min, max or count). The table shows at most 16
 * tracked columns; the dialog changes the URL only on "Apply". Key names are untrusted text.
 */
import { Columns3, Plus, X } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { useRunKeys } from "@/api/queries";
import type { RunKeyCount } from "@/api/types";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, DialogTrigger } from "@/components/ui/overlays";
import { Input, Label, NativeSelect } from "@/components/ui/primitives";

import { grammarError, labelsKey, MAX_RUN_COLUMNS, METRIC_AGGS, parseRunRef, refText, RESERVED_TAG_PREFIX, type MetricAgg } from "./run-query";

/** At most this many keys of each list show (type to find the others). */
const LIST_LIMIT = 100;

/** The column ref of a metric series (`metric.loss{split=val}.min`). */
export function metricColumn(k: Pick<RunKeyCount, "key" | "labels">, agg: MetricAgg): string {
  return refText({ kind: "metric", key: k.key, labels: Object.entries(k.labels ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)), agg });
}

/** The canonical text of a column (`null` when the grammar refuses it). */
export function canonicalColumn(text: string): string | null {
  try {
    return refText(parseRunRef(text));
  } catch {
    return null;
  }
}

function matches(k: RunKeyCount, q: string): boolean {
  if (!q) return true;
  return `${k.key} ${labelsKey(k.labels ?? {})}`.toLowerCase().includes(q);
}

function KeyList({
  title,
  items,
  refOf,
  selected,
  onToggle,
  full,
}: {
  title: string;
  items: RunKeyCount[];
  refOf: (k: RunKeyCount) => string;
  selected: readonly string[];
  onToggle: (ref: string, on: boolean) => void;
  full: boolean;
}) {
  const uid = useId();
  return (
    <section className="grid gap-1.5" aria-label={title}>
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {items.length ? (
        <ul className="grid max-h-56 gap-1 overflow-auto rounded-md border p-2">
          {items.slice(0, LIST_LIMIT).map((k, i) => {
            const ref = refOf(k);
            const on = selected.includes(ref);
            const id = `${uid}-${i}`;
            const labels = labelsKey(k.labels ?? {});
            return (
              <li key={ref} className="flex items-center gap-2">
                <Checkbox id={id} checked={on} disabled={!on && full} onCheckedChange={(v) => onToggle(ref, v === true)} />
                <Label htmlFor={id} className="min-w-0 flex-1 text-xs font-normal">
                  <span className="font-mono [overflow-wrap:anywhere]">{k.key}</span>
                  {labels ? <span className="ml-1 text-muted-foreground [overflow-wrap:anywhere]">{`{${labels}}`}</span> : null}
                </Label>
                <span className="text-[11px] whitespace-nowrap text-muted-foreground tabular">
                  {k.runs} {k.runs === 1 ? "run" : "runs"}
                </span>
              </li>
            );
          })}
          {items.length > LIST_LIMIT ? <li className="text-xs text-muted-foreground">{items.length - LIST_LIMIT} more. Type to find them.</li> : null}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">No keys match.</p>
      )}
    </section>
  );
}

export function ColumnPicker({ columns, onChange, experimentId }: { columns: readonly string[]; onChange: (columns: string[]) => void; experimentId: string | null }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [agg, setAgg] = useState<MetricAgg>("last");
  const [custom, setCustom] = useState("");
  const [customError, setCustomError] = useState<string | null>(null);
  const keys = useRunKeys(experimentId, open);
  const q = query.trim().toLowerCase();
  const lists = useMemo(() => {
    const d = keys.data;
    return {
      metrics: (d?.metrics ?? []).filter((k) => matches(k, q)),
      params: (d?.params ?? []).filter((k) => matches(k, q)),
      // platform tags (mapi.*) cannot be columns: the grammar refuses them
      tags: (d?.tags ?? []).filter((k) => !k.key.startsWith(RESERVED_TAG_PREFIX) && matches(k, q)),
    };
  }, [keys.data, q]);
  const full = draft.length >= MAX_RUN_COLUMNS;
  const toggle = (ref: string, on: boolean) => setDraft((d) => (on ? (d.includes(ref) || d.length >= MAX_RUN_COLUMNS ? d : [...d, ref]) : d.filter((x) => x !== ref)));
  const addCustom = () => {
    const t = custom.trim();
    const error = !t ? "Type a column." : grammarError(parseRunRef, t);
    if (error) {
      setCustomError(error === "Type a column." ? error : `The column is not correct: ${error}`);
      return;
    }
    toggle(canonicalColumn(t) ?? t, true);
    setCustom("");
    setCustomError(null);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (o) {
          setDraft([...columns]);
          setQuery("");
          setCustomError(null);
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Columns3 /> Columns{columns.length ? ` (${columns.length})` : ""}
        </Button>
      </DialogTrigger>
      <DialogContent wide>
        <DialogTitle>Columns</DialogTitle>
        <DialogDescription>
          Select the metrics, params and tags to show as columns. The table shows {MAX_RUN_COLUMNS} tracked columns maximum.
        </DialogDescription>
        <div className="grid gap-2" role="group" aria-label="Selected columns">
          <span className="text-xs font-medium text-muted-foreground">
            Selected: {draft.length} of {MAX_RUN_COLUMNS}
          </span>
          {draft.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {draft.map((c) => (
                <li key={c} className="inline-flex max-w-full items-center gap-1 rounded-md border py-0.5 pr-1 pl-2 font-mono text-xs">
                  <span className="[overflow-wrap:anywhere]">{c}</span>
                  <button type="button" className="rounded-sm p-0.5 text-muted-foreground hover:text-foreground" aria-label={`Remove the column ${c}`} onClick={() => toggle(c, false)}>
                    <X className="size-3" />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">No tracked columns.</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Input aria-label="Find a key" placeholder="Find a key" value={query} onChange={(e) => setQuery(e.target.value)} className="h-8 min-w-40 flex-1" />
          <NativeSelect aria-label="Metric summary" value={agg} onChange={(e) => setAgg(e.target.value as MetricAgg)} className="h-8 w-auto text-xs">
            {METRIC_AGGS.map((a) => (
              <option key={a} value={a}>
                Metric value: {a}
              </option>
            ))}
          </NativeSelect>
        </div>
        {keys.isError ? (
          <ErrorNotice error={keys.error} title="The app could not load the keys of the runs" />
        ) : keys.isPending ? (
          <LoadingRows rows={3} />
        ) : (
          <div className="grid gap-4">
            <KeyList title="Metrics" items={lists.metrics} refOf={(k) => metricColumn(k, agg)} selected={draft} onToggle={toggle} full={full} />
            <KeyList title="Params" items={lists.params} refOf={(k) => `param.${k.key}`} selected={draft} onToggle={toggle} full={full} />
            <KeyList title="Tags" items={lists.tags} refOf={(k) => `tag.${k.key}`} selected={draft} onToggle={toggle} full={full} />
            {keys.data?.truncated ? <p className="text-xs text-muted-foreground">The project has more keys than the lists show. Type a column below to add it.</p> : null}
          </div>
        )}
        <form
          className="grid gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            addCustom();
          }}
        >
          <div className="flex gap-2">
            <Input aria-label="Column by name" placeholder="metric.loss{split=val}.min" value={custom} onChange={(e) => setCustom(e.target.value)} className="h-8 flex-1 font-mono text-xs" />
            <Button type="submit" variant="outline" size="sm" disabled={full}>
              <Plus /> Add column
            </Button>
          </div>
          {customError ? <p role="alert" className="text-xs text-destructive [overflow-wrap:anywhere]">{customError}</p> : null}
        </form>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              onChange(draft);
              setOpen(false);
            }}
          >
            Apply
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
