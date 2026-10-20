import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { FlaskConical } from "lucide-react";

import { PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { RunsTable } from "@/features/runs/runs-table";

const route = getRouteApi("/runs");

/**
 * The runs workspace (v0.14): every run of the project with tracked columns, filters, saved
 * views, and the selection that opens the compare page. The table's state is the URL search.
 */
export function RunsPage() {
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/runs" });
  return (
    <>
      <PageHeader
        title="Runs"
        description="All the fit, dry-run, ingest and score runs. Add metric, param and tag columns, filter and sort them, and select runs to compare. The page updates active runs live."
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/experiments">
              <FlaskConical /> Experiments
            </Link>
          </Button>
        }
      />
      <RunsTable search={search} onSearch={(next) => void navigate({ search: next, replace: true })} />
    </>
  );
}
