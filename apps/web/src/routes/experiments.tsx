/**
 * Experiments (v0.14): named groups of runs. The list page shows the experiments of the
 * project with their run counts; an experiment's page shows its runs in the shared runs table
 * (with the experiment fixed, its own saved views) and lets a runner edit, archive or delete it.
 * An archived experiment accepts no new runs but still lists and filters. Names and
 * descriptions are untrusted text.
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { Archive, ArchiveRestore, FlaskConical, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { useDeleteExperiment, useExperiment, useExperiments, useUpdateExperiment } from "@/api/queries";
import type { Experiment } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, PageHeader, QueryView } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Switch } from "@/components/ui/overlays";
import { Badge, Card, CardContent, Label, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { ExperimentFormDialog } from "@/features/experiments/experiment-dialogs";
import { RunsTable } from "@/features/runs/runs-table";
import { absoluteTime, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

const listRoute = getRouteApi("/experiments");
const detailRoute = getRouteApi("/experiments/$experimentId");

function useCanUpdateRuns(): boolean {
  return can(useGrants(), "update", "run");
}

export function ExperimentsPage() {
  const { archived } = listRoute.useSearch();
  const navigate = useNavigate({ from: "/experiments" });
  const allowed = useCanUpdateRuns();
  const [creating, setCreating] = useState(false);
  // without "show archived" the list asks for the active experiments only
  const experiments = useExperiments(archived ? {} : { archived: false });

  return (
    <>
      <PageHeader
        title="Experiments"
        description="An experiment is a named group of runs. Put the runs that answer one question in an experiment. Then compare them and save views of their metrics."
        actions={
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex items-center gap-2">
              <Switch id="show-archived" checked={Boolean(archived)} onCheckedChange={(on) => void navigate({ search: { archived: on ? true : undefined }, replace: true })} />
              <Label htmlFor="show-archived" className="text-xs font-normal">
                Show archived experiments
              </Label>
            </span>
            {allowed ? (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus /> New experiment
              </Button>
            ) : null}
          </div>
        }
      />
      <QueryView query={experiments} errorTitle="The app could not load the experiments">
        {(page) =>
          page.items.length === 0 ? (
            <EmptyState icon={<FlaskConical />} title={archived ? "No experiments" : "No active experiments"}>
              {allowed ? "Create an experiment. Then select runs on the Runs page and add them to the experiment." : "The project has no experiments. A member with the runner role can create them."}
            </EmptyState>
          ) : (
            <Card>
              <CardContent className="pt-4">
                <Table aria-label="Experiments">
                  <THead>
                    <TR>
                      <TH>Name</TH>
                      <TH>Description</TH>
                      <TH className="text-right">Runs</TH>
                      <TH>Last run</TH>
                      <TH>Created</TH>
                    </TR>
                  </THead>
                  <TBody>
                    {page.items.map((e) => (
                      <TR key={e.id}>
                        <TD>
                          <span className="flex flex-wrap items-center gap-2">
                            <Link to="/experiments/$experimentId" params={{ experimentId: e.id }} className="font-medium hover:underline [overflow-wrap:anywhere]">
                              {e.name}
                            </Link>
                            {e.archived ? <Badge variant="secondary">archived</Badge> : null}
                          </span>
                        </TD>
                        <TD className="max-w-96 text-muted-foreground">
                          <span className="line-clamp-2 [overflow-wrap:anywhere]">{e.description ?? "—"}</span>
                        </TD>
                        <TD className="text-right tabular">{e.run_count ?? 0}</TD>
                        <TD title={e.last_run_at ?? undefined}>{e.last_run_at ? relativeTime(e.last_run_at) : "—"}</TD>
                        <TD title={absoluteTime(e.created_at)}>{relativeTime(e.created_at)}</TD>
                      </TR>
                    ))}
                  </TBody>
                </Table>
              </CardContent>
            </Card>
          )
        }
      </QueryView>
      <ExperimentFormDialog open={creating} onOpenChange={setCreating} onSaved={(exp) => void navigate({ to: "/experiments/$experimentId", params: { experimentId: exp.id } })} />
    </>
  );
}

function DeleteExperimentDialog({ experiment, open, onOpenChange }: { experiment: Experiment; open: boolean; onOpenChange: (open: boolean) => void }) {
  const remove = useDeleteExperiment();
  const navigate = useNavigate();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>Delete the experiment?</DialogTitle>
        <DialogDescription>
          The app deletes the experiment <span className="font-medium text-foreground [overflow-wrap:anywhere]">{experiment.name}</span> and its saved views. The runs stay in the project without an
          experiment. You cannot undo this.
        </DialogDescription>
        {remove.isError ? <ErrorNotice error={remove.error} title="The app could not delete the experiment" /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={remove.isPending}
            onClick={() =>
              // the promise, not mutate's callbacks: those do not run once the dialog unmounts
              void remove.mutateAsync(experiment.id).then(
                () => {
                  toast.success("The app deleted the experiment");
                  void navigate({ to: "/experiments" });
                },
                () => undefined, // shown in the dialog (remove.isError)
              )
            }
          >
            <Trash2 /> Delete experiment
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ExperimentPage() {
  const { experimentId } = detailRoute.useParams();
  const search = detailRoute.useSearch();
  const navigate = useNavigate({ from: "/experiments/$experimentId" });
  const allowed = useCanUpdateRuns();
  const experiment = useExperiment(experimentId);
  const update = useUpdateExperiment();
  const [dialog, setDialog] = useState<"edit" | "delete" | null>(null);

  if (experiment.isPending) return <LoadingRows rows={6} />;
  if (experiment.isError) return <ErrorNotice error={experiment.error} title="The app could not load this experiment" />;
  const e = experiment.data;

  return (
    <>
      <PageHeader
        title={<span className="[overflow-wrap:anywhere]">{e.name}</span>}
        description={e.description ? <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{e.description}</span> : undefined}
        meta={
          <>
            <Link to="/experiments" className="hover:underline">
              Experiments
            </Link>
            <span>
              {e.run_count ?? 0} {(e.run_count ?? 0) === 1 ? "run" : "runs"}
            </span>
            <span title={absoluteTime(e.created_at)}>created {relativeTime(e.created_at)}</span>
            {e.archived ? <Badge variant="secondary">archived</Badge> : null}
          </>
        }
        actions={
          allowed ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setDialog("edit")}>
                <Pencil /> Edit
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={update.isPending}
                onClick={() =>
                  update.mutate(
                    { id: e.id, patch: { archived: !e.archived } },
                    {
                      onSuccess: (x) => toast.success(x.archived ? "The app archived the experiment" : "The app restored the experiment"),
                      onError: (err) => toast.error("The app could not change the experiment", err.message),
                    },
                  )
                }
              >
                {e.archived ? (
                  <>
                    <ArchiveRestore /> Restore
                  </>
                ) : (
                  <>
                    <Archive /> Archive
                  </>
                )}
              </Button>
              <Button variant="outline" size="sm" onClick={() => setDialog("delete")}>
                <Trash2 /> Delete
              </Button>
            </>
          ) : null
        }
      />
      {e.archived ? (
        <p role="status" className="mb-4 rounded-md border border-dashed px-3 py-2 text-[13px] text-muted-foreground">
          The experiment is archived. It accepts no new runs. Restore it to add runs.
        </p>
      ) : null}
      <RunsTable
        search={search}
        experimentId={e.id}
        onSearch={(next) => void navigate({ search: { ...next, experiment: undefined }, replace: true })}
        emptyState={
          <EmptyState icon={<FlaskConical />} title="The experiment has no runs">
            Select runs on the Runs page and add them to the experiment. You can also name the experiment when you start a run with the CLI or the API.
          </EmptyState>
        }
      />
      <ExperimentFormDialog experiment={e} open={dialog === "edit"} onOpenChange={(o) => setDialog(o ? "edit" : null)} />
      <DeleteExperimentDialog experiment={e} open={dialog === "delete"} onOpenChange={(o) => setDialog(o ? "delete" : null)} />
    </>
  );
}
