import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, FileCode2, Plus, XCircle } from "lucide-react";

import { useSpecs } from "@/api/queries";
import type { SpecDoc } from "@/api/types";
import { EmptyState, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { ListNoMatches, ListToolbar, matchesQuery, toolbarShown } from "@/components/list-toolbar";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { relativeTime } from "@/lib/format";

const columns: ColumnDef<SpecDoc, unknown>[] = [
  {
    accessorKey: "name",
    header: "Spec",
    cell: ({ row }) => (
      <span className="grid">
        <span className="font-medium">{row.original.name}</span>
        <Mono className="text-muted-foreground">{row.original.ref}</Mono>
      </span>
    ),
  },
  { accessorKey: "kind", header: "Kind", cell: ({ getValue }) => <Badge variant="outline">{String(getValue())}</Badge> },
  { accessorKey: "latest_revision", header: "Revision", cell: ({ getValue }) => <Mono>r{String(getValue())}</Mono> },
  {
    id: "valid",
    header: "Latest",
    accessorFn: (s) => s.latest?.valid ?? null,
    cell: ({ row }) =>
      row.original.latest == null ? (
        <span className="text-muted-foreground">—</span>
      ) : row.original.latest.valid ? (
        <span className="inline-flex items-center gap-1 text-success"><CheckCircle2 className="size-3.5" /> valid</span>
      ) : (
        <span className="inline-flex items-center gap-1 text-destructive"><XCircle className="size-3.5" /> {row.original.latest.issues?.length ?? 0} issues</span>
      ),
  },
  { accessorKey: "updated_at", header: "Updated", cell: ({ row }) => relativeTime(row.original.updated_at) },
];

const listRoute = getRouteApi("/build");

export function BuildPage() {
  const specs = useSpecs();
  const navigate = useNavigate();
  const q = listRoute.useSearch().q ?? "";
  const setQuery = (next: string) => void navigate({ to: "/build", search: { q: next || undefined }, replace: true });
  const items = specs.data?.items;
  const more = Boolean(specs.data?.next_cursor);
  const rows = q && items ? items.filter((s) => matchesQuery(q, [s.name], [s.ref, s.kind])) : (items ?? []);
  return (
    <>
      <PageHeader
        title="Build"
        description="A model spec is a YAML document. You cannot change a revision after you save it. The server does a check of each revision before a run starts."
        actions={
          <Button asChild>
            <Link to="/build/new">
              <Plus /> New spec
            </Link>
          </Button>
        }
      />
      {/* A block, not a grid: a grid item around the table would take the width of the table on a phone. */}
      <div className="space-y-3">
        {/* Outside the query view: a link with ?q= shows its text while the list loads and when the project has no spec. */}
        {toolbarShown(q, items?.length) ? <ListToolbar things="specs" value={q} onChange={setQuery} count={items ? { shown: rows.length, total: items.length, more } : undefined} /> : null}
        <QueryView query={specs}>
          {(page) => {
            return page.items.length ? (
              <div className="grid gap-3">
                {rows.length ? (
                  <DataTable
                    label="Specs"
                    data={rows}
                    columns={columns}
                    getRowId={(s) => s.id}
                    onRowClick={(s) => void navigate({ to: "/build/$specId", params: { specId: s.id } })}
                  />
                ) : (
                  <ListNoMatches things="specs" query={q} firstPage={more ? page.items.length : undefined} onClear={() => setQuery("")} />
                )}
              </div>
            ) : (
              <EmptyState
                icon={<FileCode2 />}
                title="No specs yet"
                action={
                  <Button asChild>
                    <Link to="/build/new">
                      <Plus /> Start from the geo MMM template
                    </Link>
                  </Button>
                }
              >
                A spec names a dataset, a preprocessing pipeline and a model plugin. The app does a check of the spec while you type.
              </EmptyState>
            );
          }}
        </QueryView>
      </div>
    </>
  );
}
