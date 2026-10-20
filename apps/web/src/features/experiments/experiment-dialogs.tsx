/**
 * Experiment dialogs (v0.14): create or edit an experiment (name and description), and add
 * runs to an experiment (the runs table's "Add to experiment"). Experiment names are unique in
 * the project (case is not significant): the server answers 409, which the form shows on the
 * name. Names and descriptions are untrusted text.
 */
import { useState } from "react";

import { ApiError } from "@/api/client";
import { MAX_EXPERIMENT_DESCRIPTION_CHARS, MAX_EXPERIMENT_NAME_CHARS, MAX_EXPERIMENT_RUNS_WRITE } from "@/api/experiments";
import { useCreateExperiment, useExperiments, useUpdateExperiment, useUpdateExperimentRuns } from "@/api/queries";
import type { Experiment } from "@/api/types";
import { ErrorNotice } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";
import { toast } from "@/stores/toast";

/** The form problem of an experiment name, in STE ("" when it is valid). */
export function experimentNameError(name: string): string {
  const t = name.trim();
  if (!t) return "Type a name.";
  if (t.length > MAX_EXPERIMENT_NAME_CHARS) return `Use ${MAX_EXPERIMENT_NAME_CHARS} characters or fewer.`;
  if (/[\u0000-\u001f\u007f]/.test(t)) return "Do not use control characters.";
  return "";
}

function conflict(err: unknown): string | null {
  return err instanceof ApiError && err.status === 409 ? "An experiment with this name exists. Type a different name." : null;
}

/** "New experiment" (no `experiment`) or "Edit the experiment". */
export function ExperimentFormDialog({
  experiment,
  open,
  onOpenChange,
  onSaved,
}: {
  experiment?: Experiment | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved?: ((experiment: Experiment) => void) | undefined;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>{experiment ? "Edit the experiment" : "New experiment"}</DialogTitle>
        <DialogDescription>An experiment is a named group of runs. Put the runs that you want to compare in the same experiment.</DialogDescription>
        {/* The content mounts on open: the form starts again from the experiment each time. */}
        <ExperimentForm experiment={experiment} onClose={() => onOpenChange(false)} onSaved={onSaved} />
      </DialogContent>
    </Dialog>
  );
}

function ExperimentForm({ experiment, onClose, onSaved }: { experiment?: Experiment | undefined; onClose: () => void; onSaved?: ((experiment: Experiment) => void) | undefined }) {
  const create = useCreateExperiment();
  const update = useUpdateExperiment();
  const [name, setName] = useState(experiment?.name ?? "");
  const [description, setDescription] = useState(experiment?.description ?? "");
  const [touched, setTouched] = useState(false);
  const mutation = experiment ? update : create;
  const nameProblem = touched ? experimentNameError(name) : "";
  const taken = conflict(mutation.error);

  const save = () => {
    setTouched(true);
    if (experimentNameError(name) || description.length > MAX_EXPERIMENT_DESCRIPTION_CHARS) return;
    const body = { name: name.trim(), description: description.trim() ? description : null };
    const onSuccess = (exp: Experiment) => {
      onClose();
      toast.success(experiment ? "The app saved the experiment" : "The app created the experiment");
      onSaved?.(exp);
    };
    if (experiment) update.mutate({ id: experiment.id, patch: body }, { onSuccess });
    else create.mutate(body, { onSuccess });
  };

  return (
    <form
      className="grid gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <Field label="Name" htmlFor="experiment-name" error={nameProblem || taken} hint={`A unique name in the project (${MAX_EXPERIMENT_NAME_CHARS} characters maximum).`}>
        <Input id="experiment-name" value={name} maxLength={MAX_EXPERIMENT_NAME_CHARS} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>
      <Field
        label="Description"
        htmlFor="experiment-description"
        error={description.length > MAX_EXPERIMENT_DESCRIPTION_CHARS ? `Use ${MAX_EXPERIMENT_DESCRIPTION_CHARS.toLocaleString("en")} characters or fewer.` : undefined}
        hint="Write the question that the runs answer. This is optional."
      >
        <Textarea id="experiment-description" value={description} onChange={(e) => setDescription(e.target.value)} rows={4} />
      </Field>
      {mutation.isError && !taken ? <ErrorNotice error={mutation.error} title="The app could not save the experiment" /> : null}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={mutation.isPending}>
          {experiment ? "Save" : "Create experiment"}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** "Add to experiment": move the selected runs into an experiment (not archived). */
export function AddToExperimentDialog({
  runIds,
  open,
  onOpenChange,
  onDone,
}: {
  runIds: readonly string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone?: (() => void) | undefined;
}) {
  const experiments = useExperiments({ archived: false });
  const write = useUpdateExperimentRuns();
  const [target, setTarget] = useState("");
  const [creating, setCreating] = useState(false);
  const items = (experiments.data?.items ?? []).filter((e) => !e.archived);
  const tooMany = runIds.length > MAX_EXPERIMENT_RUNS_WRITE;

  const add = (experimentId: string) =>
    write.mutate(
      { id: experimentId, body: { add: [...runIds] } },
      {
        onSuccess: (res) => {
          onOpenChange(false);
          onDone?.();
          const missing = res.missing?.length ?? 0;
          toast.success(
            `The app added ${res.added} ${res.added === 1 ? "run" : "runs"} to the experiment`,
            missing ? `${missing} ${missing === 1 ? "run is" : "runs are"} not in this project.` : undefined,
          );
        },
      },
    );

  return (
    <>
      <Dialog
        open={open && !creating}
        onOpenChange={(o) => {
          onOpenChange(o);
          if (o) write.reset();
        }}
      >
        <DialogContent>
          <DialogTitle>Add to experiment</DialogTitle>
          <DialogDescription>
            Put the {runIds.length} selected {runIds.length === 1 ? "run" : "runs"} in an experiment. A run is in one experiment only: the app moves runs that are in a different experiment.
          </DialogDescription>
          <form
            className="grid gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (target) add(target);
            }}
          >
            <Field label="Experiment" htmlFor="add-experiment" hint={items.length ? undefined : "The project has no experiments. Create one."}>
              <NativeSelect id="add-experiment" value={target} onChange={(e) => setTarget(e.target.value)} disabled={experiments.isPending}>
                <option value="">Select an experiment</option>
                {items.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            {tooMany ? <p className="text-xs text-destructive">Select {MAX_EXPERIMENT_RUNS_WRITE} runs or fewer.</p> : null}
            {experiments.isError ? <ErrorNotice error={experiments.error} title="The app could not load the experiments" /> : null}
            {write.isError ? <ErrorNotice error={write.error} title="The app could not add the runs" /> : null}
            <DialogFooter className="flex-wrap">
              <Button type="button" variant="ghost" onClick={() => setCreating(true)}>
                New experiment
              </Button>
              <Button type="submit" disabled={!target || tooMany || write.isPending}>
                Add runs
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <ExperimentFormDialog
        open={open && creating}
        onOpenChange={(o) => {
          if (!o) setCreating(false);
        }}
        onSaved={(exp) => {
          setCreating(false);
          setTarget(exp.id);
        }}
      />
    </>
  );
}
