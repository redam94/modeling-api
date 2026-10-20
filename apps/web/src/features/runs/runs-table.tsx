/**
 * The runs table of the runs workspace and the experiment page (v0.14). The URL search holds
 * its state (I-12): state, kind, experiment, name, filters (`f`), tracked columns (`c`), the
 * order (`o`) and the applied saved view. The server filters, sorts and pages (`GET /v1/runs`
 * with `filter`/`order_by`/`column` and a keyset cursor: "Load more"); the table only shows
 * what it answers.
 *
 * Rows can be selected (on every loaded page) to compare 2..10 runs or to add them to an
 * experiment. Names, tags, column values and experiment names are untrusted text.
 */
import { Link, useNavigate } from "@tanstack/react-router";
import { Activity, ArrowDown, ArrowUp, ArrowUpDown, FlaskConical, GitCompareArrows, Search, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { can } from "@/api/access";
import { ApiError } from "@/api/client";
import { MAX_COMPARE_RUNS, MIN_COMPARE_RUNS } from "@/api/experiments";
import { useExperiments, useRunPages } from "@/api/queries";
import { runName, runTags } from "@/api/tracking";
import type { Experiment, Run, RunKind, RunState } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, Mono, StateBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Input, NativeSelect, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { AddToExperimentDialog } from "@/features/experiments/experiment-dialogs";
import { useDebounced } from "@/hooks/use-debounced";
import { runCostView } from "@/lib/cost";
import { elapsedSeconds, formatDuration, formatNumber, relativeTime, shortId } from "@/lib/format";
import { RUN_KINDS, RUN_STATES, runSubject, STATE_META } from "@/lib/run-state";
import { cn } from "@/lib/utils";

import { ColumnPicker } from "./column-picker";
import { FilterBar } from "./filter-bar";
import { parseRunOrder, parseRunRef, refLabel, refText, type RunRef } from "./run-query";
import { compactSearch, filtersFromSearch, hasRunFilters, type RunsSearch } from "./run-search";
import { SavedViews } from "./saved-views";
import { TagBadges } from "./tag-badges";

export interface TrackedColumn {
  /** The text in the URL (`c`). */
  text: string;
  /** Canonical text: the key of `Run.columns` and of `order_by`. */
  key: string;
  ref: RunRef;
  label: string;
}

/** The tracked columns of `c` (columns the grammar refuses are left out: the server says why). */
export function trackedColumns(texts: readonly string[] | undefined): TrackedColumn[] {
  return (texts ?? []).flatMap((text) => {
    try {
      const ref = parseRunRef(text);
      return [{ text, key: refText(ref), ref, label: refLabel(ref) }];
    } catch {
      return [];
    }
  });
}

/** The canonical order `{ key, descending }` of `o`, or `null` (the server's default: newest first). */
export function currentOrder(o: string | undefined): { key: string; descending: boolean } | null {
  if (!o) return null;
  try {
    const order = parseRunOrder(o);
    return { key: refText(order.ref), descending: order.descending };
  } catch {
    return null;
  }
}

/** The next `o` when the header of column `key` is clicked: ascending, then descending, then none. */
export function nextOrder(o: string | undefined, key: string): string | undefined {
  const cur = currentOrder(o);
  if (!cur || cur.key !== key) return key;
  return cur.descending ? undefined : `-${key}`;
}

/** The text of a `Run.columns` value: numbers formatted, text as is, "—" when the run has none. */
export function columnText(v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  return typeof v === "number" ? formatNumber(v) : v;
}

/** The new selection after a checkbox click (in click order; at most what a compare takes). */
export function toggleSelection(selected: readonly string[], id: string, on: boolean): string[] {
  if (!on) return selected.filter((x) => x !== id);
  return selected.includes(id) ? [...selected] : [...selected, id];
}

/** The 422 message of a refused search (the server's grammar message), or `null`. */
export function searchProblem(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  return err.problem?.detail ?? err.problem?.title ?? err.message;
}

function SortHeader({ label, sortKey, order, onSort, className }: { label: ReactNode; sortKey: string; order: { key: string; descending: boolean } | null; onSort: (key: string) => void; className?: string }) {
  const active = order?.key === sortKey ? order : null;
  return (
    <TH aria-sort={active ? (active.descending ? "descending" : "ascending") : undefined} className={className}>
      <button type="button" className="inline-flex items-center gap-1 hover:text-foreground" onClick={() => onSort(sortKey)}>
        {label}
        {active ? active.descending ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" /> : <ArrowUpDown className="size-3 opacity-40" />}
      </button>
    </TH>
  );
}

function ExperimentCell({ id, experiments }: { id: string | null | undefined; experiments: Map<string, Experiment> }) {
  if (!id) return <span className="text-muted-foreground">—</span>;
  const exp = experiments.get(id);
  return (
    <Link to="/experiments/$experimentId" params={{ experimentId: id }} className="inline-block max-w-48 truncate align-bottom hover:underline" title={exp?.name ?? id}>
      {exp ? exp.name : <Mono>{shortId(id)}</Mono>}
    </Link>
  );
}

export function RunsTable({
  search,
  onSearch,
  experimentId,
  emptyState,
}: {
  search: RunsSearch;
  /** Write a new search to the URL. */
  onSearch: (next: RunsSearch) => void;
  /** The experiment the page fixes (the experiment page): no experiment select or column. */
  experimentId?: string | undefined;
  /** Shown when the project (or experiment) has no runs and no filter applies. */
  emptyState?: ReactNode;
}) {
  const navigate = useNavigate();
  const grants = useGrants();
  const canUpdate = can(grants, "update", "run");
  const filters = useMemo(() => filtersFromSearch(search, experimentId), [search, experimentId]);
  const runs = useRunPages(filters);
  const experiments = useExperiments({});
  const experimentMap = useMemo(() => new Map((experiments.data?.items ?? []).map((e) => [e.id, e])), [experiments.data]);
  const columns = useMemo(() => trackedColumns(search.c), [search.c]);
  const order = currentOrder(search.o);
  const [selected, setSelected] = useState<string[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState(search.name ?? "");
  const debouncedName = useDebounced(name, 400);
  const set = (patch: Partial<RunsSearch>) => onSearch(compactSearch({ ...search, ...patch }));

  // The name box writes the URL after a pause; a new URL (a view, Back) fills the box.
  useEffect(() => {
    if ((debouncedName.trim() || undefined) !== (search.name ?? undefined)) set({ name: debouncedName.trim() || undefined });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only the debounced value starts a write
  }, [debouncedName]);
  useEffect(() => {
    setName((n) => ((n.trim() || undefined) === (search.name ?? undefined) ? n : (search.name ?? "")));
  }, [search.name]);

  const items = useMemo(() => runs.data?.pages.flatMap((p) => p.items) ?? [], [runs.data]);
  const problem = searchProblem(runs.error);
  const onSort = (key: string) => set({ o: nextOrder(search.o, key) });
  const pageIds = items.map((r) => r.id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.includes(id));
  const canCompare = selected.length >= MIN_COMPARE_RUNS && selected.length <= MAX_COMPARE_RUNS;
  const filtered = hasRunFilters(search, experimentId);

  return (
    <div className="grid min-w-0 gap-4">
      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Run filters">
        {experimentId ? null : (
          <NativeSelect aria-label="Filter by experiment" value={search.experiment ?? ""} onChange={(e) => set({ experiment: e.target.value || undefined })} className="h-8 w-48">
            <option value="">All experiments</option>
            {(experiments.data?.items ?? []).map((e) => (
              <option key={e.id} value={e.id}>
                {e.archived ? `${e.name} (archived)` : e.name}
              </option>
            ))}
          </NativeSelect>
        )}
        <NativeSelect aria-label="Filter by state" value={search.state ?? ""} onChange={(e) => set({ state: (e.target.value || undefined) as RunState | undefined })} className="h-8 w-40">
          <option value="">All states</option>
          {RUN_STATES.map((s) => (
            <option key={s} value={s}>
              {STATE_META[s].label}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect aria-label="Filter by kind" value={search.kind ?? ""} onChange={(e) => set({ kind: (e.target.value || undefined) as RunKind | undefined })} className="h-8 w-36">
          <option value="">All kinds</option>
          {RUN_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </NativeSelect>
        <div className="relative min-w-40 flex-1 sm:max-w-64">
          <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-muted-foreground" />
          <Input aria-label="Find runs by name" placeholder="Find runs by name" value={name} onChange={(e) => setName(e.target.value)} className="h-8 pl-8" />
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <ColumnPicker columns={search.c ?? []} onChange={(c) => set({ c })} experimentId={experimentId ?? search.experiment ?? null} />
          <SavedViews search={search} experimentId={experimentId} onApply={onSearch} />
        </div>
      </div>

      <FilterBar filters={search.f ?? []} onChange={(f) => set({ f })} serverError={problem} />

      <div className="flex flex-wrap items-center gap-2" aria-live="polite">
        <span className="text-xs text-muted-foreground">
          {selected.length ? `${selected.length} ${selected.length === 1 ? "run" : "runs"} selected` : "Select runs to compare them or to add them to an experiment."}
        </span>
        <Button size="sm" disabled={!canCompare} onClick={() => void navigate({ to: "/runs/compare", search: { runs: selected } })} title={canCompare ? undefined : `Select ${MIN_COMPARE_RUNS} to ${MAX_COMPARE_RUNS} runs`}>
          <GitCompareArrows /> Compare{selected.length ? ` (${selected.length})` : ""}
        </Button>
        {canUpdate ? (
          <Button size="sm" variant="outline" disabled={!selected.length} onClick={() => setAdding(true)}>
            <FlaskConical /> Add to experiment
          </Button>
        ) : null}
        {selected.length ? (
          <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
            <X /> Clear the selection
          </Button>
        ) : null}
        {selected.length > MAX_COMPARE_RUNS ? <span className="text-xs text-warning">You can compare {MAX_COMPARE_RUNS} runs maximum.</span> : null}
      </div>

      {runs.isPending ? (
        <LoadingRows rows={6} />
      ) : runs.isError && !runs.data ? (
        problem ? null : <ErrorNotice error={runs.error} title="The app could not load the runs" />
      ) : !items.length && !filtered ? (
        (emptyState ?? (
          <EmptyState icon={<Activity />} title="No runs yet">
            Fit a spec on the Build page. Or upload a dataset to start an ingest run.
          </EmptyState>
        ))
      ) : (
        <div className="grid min-w-0 gap-3">
          <div className="rounded-lg border bg-card">
            <Table aria-label="Runs">
              <THead>
                <TR>
                  <TH className="w-8">
                    <Checkbox
                      aria-label="Select all the runs on the page"
                      checked={allOnPage}
                      disabled={!pageIds.length}
                      onCheckedChange={(on) => setSelected((s) => (on ? [...s, ...pageIds.filter((id) => !s.includes(id))] : s.filter((id) => !pageIds.includes(id))))}
                    />
                  </TH>
                  <TH>State</TH>
                  <SortHeader label="Name" sortKey="name" order={order} onSort={onSort} />
                  {experimentId ? null : <TH>Experiment</TH>}
                  <TH>Kind</TH>
                  <TH>Input</TH>
                  <TH>Duration</TH>
                  <TH>Cost</TH>
                  <SortHeader label="Created" sortKey="created_at" order={order} onSort={onSort} />
                  {columns.map((c) => (
                    <SortHeader key={c.key} label={<span className="max-w-48 truncate font-mono" title={c.key}>{c.label}</span>} sortKey={c.key} order={order} onSort={onSort} className="text-right" />
                  ))}
                </TR>
              </THead>
              <TBody>
                {items.map((r) => (
                  <RunRow
                    key={r.id}
                    run={r}
                    columns={columns}
                    experiments={experimentMap}
                    showExperiment={!experimentId}
                    selected={selected.includes(r.id)}
                    onSelect={(on) => setSelected((s) => toggleSelection(s, r.id, on))}
                  />
                ))}
              </TBody>
            </Table>
            {!items.length ? <p className="p-6 text-muted-foreground">No runs match these filters. Change the filters.</p> : null}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {runs.hasNextPage ? (
              <Button variant="outline" size="sm" disabled={runs.isFetchingNextPage} onClick={() => void runs.fetchNextPage()}>
                {runs.isFetchingNextPage ? "Loading…" : "Load more runs"}
              </Button>
            ) : null}
            <span className="text-xs text-muted-foreground">
              {items.length} {items.length === 1 ? "run" : "runs"} shown{runs.hasNextPage ? ". More runs match." : "."}
            </span>
          </div>
        </div>
      )}

      {adding ? (
        <AddToExperimentDialog
          runIds={selected}
          open
          onOpenChange={(o) => {
            if (!o) setAdding(false);
          }}
          onDone={() => setSelected([])}
        />
      ) : null}
    </div>
  );
}

function RunRow({
  run,
  columns,
  experiments,
  showExperiment,
  selected,
  onSelect,
}: {
  run: Run;
  columns: TrackedColumn[];
  experiments: Map<string, Experiment>;
  showExperiment: boolean;
  selected: boolean;
  onSelect: (on: boolean) => void;
}) {
  const name = runName(run);
  const tags = runTags(run);
  const cost = runCostView(run.cost);
  return (
    <TR data-state={selected ? "selected" : undefined}>
      <TD>
        <Checkbox aria-label={`Select the run ${name ?? run.id}`} checked={selected} onCheckedChange={(on) => onSelect(on === true)} />
      </TD>
      <TD>
        <StateBadge state={run.state} />
      </TD>
      <TD>
        {/* v0.11: the run's name (or its short id) and up to three tags. Both are untrusted text. */}
        <span className="grid max-w-80 min-w-32 gap-1">
          <Link to="/runs/$runId" params={{ runId: run.id }} className="hover:underline">
            {name ? (
              <span className="font-medium [overflow-wrap:anywhere]" title={run.id}>
                {name}
              </span>
            ) : (
              <Mono>{shortId(run.id)}</Mono>
            )}
          </Link>
          {Object.keys(tags).length ? <TagBadges tags={tags} max={3} /> : null}
        </span>
      </TD>
      {showExperiment ? (
        <TD>
          <ExperimentCell id={run.experiment_id} experiments={experiments} />
        </TD>
      ) : null}
      <TD>{run.kind}</TD>
      <TD>
        <Mono className="text-muted-foreground">{runSubject(run)}</Mono>
      </TD>
      <TD>
        <span className="tabular">{formatDuration(elapsedSeconds(run.started_at, run.finished_at))}</span>
      </TD>
      <TD>
        {cost ? (
          <span className={cost.estimated ? "text-muted-foreground tabular" : "tabular"} title={cost.detail}>
            {cost.text}
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TD>
      <TD>
        <span title={run.created_at}>{relativeTime(run.created_at)}</span>
      </TD>
      {columns.map((c) => {
        const v = run.columns?.[c.key];
        return (
          <TD key={c.key} className={cn("text-right", typeof v === "number" && "tabular")}>
            {/* numbers stay on one line; params and tags (text) may wrap anywhere */}
            <span className={cn("inline-block max-w-56", typeof v === "number" ? "whitespace-nowrap" : "[overflow-wrap:anywhere]")}>{columnText(v)}</span>
          </TD>
        );
      })}
    </TR>
  );
}
