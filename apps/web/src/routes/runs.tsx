import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { Activity } from "lucide-react";
import { useMemo } from "react";

import { useRuns } from "@/api/queries";
import type { Run, RunKind, RunState } from "@/api/types";
import { EmptyState, Mono, PageHeader, QueryView, StateBadge } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { NativeSelect } from "@/components/ui/primitives";
import { elapsedSeconds, formatDuration, relativeTime, shortId } from "@/lib/format";
import { runCostView } from "@/lib/cost";
import { RUN_KINDS, RUN_STATES, runSubject, STATE_META } from "@/lib/run-state";

const route = getRouteApi("/runs");

export const runColumns: ColumnDef<Run, unknown>[] = [
  {
    accessorKey: "state",
    header: "State",
    cell: ({ row }) => <StateBadge state={row.original.state} />,
    sortingFn: (a, b) => RUN_STATES.indexOf(a.original.state) - RUN_STATES.indexOf(b.original.state),
  },
  {
    accessorKey: "id",
    header: "Run",
    cell: ({ row }) => (
      <Link to="/runs/$runId" params={{ runId: row.original.id }} className="hover:underline" onClick={(e) => e.stopPropagation()}>
        <Mono>{shortId(row.original.id)}</Mono>
      </Link>
    ),
  },
  { accessorKey: "kind", header: "Kind" },
  { id: "subject", header: "Input", accessorFn: runSubject, cell: ({ getValue }) => <Mono className="text-muted-foreground">{String(getValue())}</Mono> },
  { accessorKey: "resource_class", header: "Class" },
  {
    id: "duration",
    header: "Duration",
    accessorFn: (r) => elapsedSeconds(r.started_at, r.finished_at) ?? -1,
    cell: ({ row }) => <span className="tabular">{formatDuration(elapsedSeconds(row.original.started_at, row.original.finished_at))}</span>,
  },
  {
    id: "cost",
    header: "Cost",
    accessorFn: (r) => r.cost?.amount_micros ?? r.cost?.estimate_micros ?? -1,
    cell: ({ row }) => {
      const c = runCostView(row.original.cost);
      return c ? (
        <span className={c.estimated ? "text-muted-foreground tabular" : "tabular"} title={c.detail}>
          {c.text}
        </span>
      ) : (
        <span className="text-muted-foreground">—</span>
      );
    },
  },
  {
    accessorKey: "created_at",
    header: "Created",
    cell: ({ row }) => <span title={row.original.created_at}>{relativeTime(row.original.created_at)}</span>,
  },
];

export function RunsPage() {
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/runs" });
  const filters = useMemo(() => ({ ...(search.state ? { state: search.state } : {}), ...(search.kind ? { kind: search.kind } : {}) }), [search.state, search.kind]);
  const runs = useRuns(filters);
  return (
    <>
      <PageHeader
        title="Runs"
        description="Every fit, dry-run, ingest and score. Active runs update live."
        actions={
          <div className="flex gap-2">
            <NativeSelect
              aria-label="Filter by state"
              value={search.state ?? ""}
              onChange={(e) => void navigate({ search: (s) => ({ ...s, state: (e.target.value || undefined) as RunState | undefined }) })}
              className="h-8 w-40"
            >
              <option value="">All states</option>
              {RUN_STATES.map((s) => (
                <option key={s} value={s}>
                  {STATE_META[s].label}
                </option>
              ))}
            </NativeSelect>
            <NativeSelect
              aria-label="Filter by kind"
              value={search.kind ?? ""}
              onChange={(e) => void navigate({ search: (s) => ({ ...s, kind: (e.target.value || undefined) as RunKind | undefined }) })}
              className="h-8 w-36"
            >
              <option value="">All kinds</option>
              {RUN_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </NativeSelect>
          </div>
        }
      />
      <QueryView query={runs}>
        {(page) =>
          page.items.length === 0 && !search.state && !search.kind ? (
            <EmptyState icon={<Activity />} title="No runs yet">
              Fit a spec from Build, or upload data to start an ingest run.
            </EmptyState>
          ) : (
            <DataTable
              label="Runs"
              data={page.items}
              columns={runColumns}
              getRowId={(r) => r.id}
              onRowClick={(r) => void navigate({ to: "/runs/$runId", params: { runId: r.id } })}
              empty="No runs match these filters."
            />
          )
        }
      </QueryView>
    </>
  );
}
