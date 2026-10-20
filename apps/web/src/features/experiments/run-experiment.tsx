/**
 * The "Experiment" card of the run page's Notes tab (v0.14): the experiment of the run, and a
 * select that moves the run to another experiment or takes it out (`PATCH /v1/runs/{id}` with
 * `experiment_id`). Archived experiments accept no new runs: they are not in the select. Without
 * the `update` permission on runs the card is read-only.
 */
import { Link } from "@tanstack/react-router";
import { FlaskConical } from "lucide-react";

import { can } from "@/api/access";
import { useExperiment, useExperiments, useUpdateRun } from "@/api/queries";
import type { Run } from "@/api/types";
import { ErrorNotice } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle, Field, NativeSelect } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

export function RunExperiment({ run }: { run: Run }) {
  const allowed = can(useGrants(), "update", "run");
  const experiments = useExperiments({});
  const update = useUpdateRun();
  const items = experiments.data?.items ?? [];
  const current = run.experiment_id ? items.find((e) => e.id === run.experiment_id) : undefined;
  const link = run.experiment_id ? (
    <Link to="/experiments/$experimentId" params={{ experimentId: run.experiment_id }} className="text-primary hover:underline [overflow-wrap:anywhere]">
      {current?.name ?? run.experiment_id}
    </Link>
  ) : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FlaskConical className="size-4 text-muted-foreground" /> Experiment
        </CardTitle>
      </CardHeader>
      <CardContent className="grid max-w-3xl gap-3 text-[13px]">
        {allowed ? (
          <Field label="Experiment of the run" htmlFor="run-experiment" hint="A run is in one experiment only. Compare the runs of an experiment on its page.">
            <NativeSelect
              id="run-experiment"
              value={run.experiment_id ?? ""}
              disabled={experiments.isPending || update.isPending}
              onChange={(e) =>
                update.mutate(
                  { id: run.id, patch: { experiment_id: e.target.value || null } },
                  { onSuccess: () => toast.success(e.target.value ? "The app moved the run to the experiment" : "The app took the run out of the experiment") },
                )
              }
            >
              <option value="">No experiment</option>
              {items
                .filter((e) => !e.archived || e.id === run.experiment_id)
                .map((e) => (
                  <option key={e.id} value={e.id} disabled={e.archived}>
                    {e.archived ? `${e.name} (archived)` : e.name}
                  </option>
                ))}
            </NativeSelect>
          </Field>
        ) : (
          <p>{link ?? <span className="text-muted-foreground">The run is not in an experiment.</span>}</p>
        )}
        {allowed && link ? <p className="text-xs text-muted-foreground">Open the experiment: {link}</p> : null}
        {experiments.isError ? <ErrorNotice error={experiments.error} title="The app could not load the experiments" /> : null}
        {update.isError ? <ErrorNotice error={update.error} title="The app could not change the experiment" /> : null}
      </CardContent>
    </Card>
  );
}

/** The run's experiment as a link (the Details card), "—" without one. */
export function RunExperimentLink({ experimentId }: { experimentId: string | null | undefined }) {
  const experiment = useExperiment(experimentId ?? undefined);
  if (!experimentId) return <span>—</span>;
  return (
    <Link to="/experiments/$experimentId" params={{ experimentId }} className="hover:underline [overflow-wrap:anywhere]">
      {experiment.data?.name ?? experimentId}
    </Link>
  );
}
