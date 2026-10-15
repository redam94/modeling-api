import { Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, FileCode2, Plus, XCircle } from "lucide-react";

import { useSpecs } from "@/api/queries";
import type { SpecDoc } from "@/api/types";
import { EmptyState, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
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

export function BuildPage() {
  const specs = useSpecs();
  const navigate = useNavigate();
  return (
    <>
      <PageHeader
        title="Build"
        description="Model specs are YAML documents. Every revision is immutable and validated statically before anything runs."
        actions={
          <Button asChild>
            <Link to="/build/new">
              <Plus /> New spec
            </Link>
          </Button>
        }
      />
      <QueryView query={specs}>
        {(page) =>
          page.items.length ? (
            <DataTable
              label="Specs"
              data={page.items}
              columns={columns}
              getRowId={(s) => s.id}
              filterPlaceholder="Filter specs"
              onRowClick={(s) => void navigate({ to: "/build/$specId", params: { specId: s.id } })}
            />
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
              A spec names a dataset, a preprocessing pipeline and a model plugin. Validation runs as you type.
            </EmptyState>
          )
        }
      </QueryView>
    </>
  );
}
