/**
 * Start a run: bind the inputs, adjust parameters, reuse cached results or not. The request
 * carries an Idempotency-Key per intent, so a network retry never starts a second run.
 */
import { useNavigate } from "@tanstack/react-router";
import { Play } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { ApiError, newIdempotencyKey } from "@/api/client";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import type { JsonObject } from "@/features/forms/json-schema-form";

import { useStartWorkflowRun } from "./api";
import { InputsForm, missingInputs, missingParams, ParamsForm } from "./run-inputs";
import type { WorkflowDoc, WorkflowRevision } from "./types";

export function RunDialog({ workflowId, revision, onClose }: { workflowId: string; revision: WorkflowRevision; onClose: () => void }) {
  const navigate = useNavigate();
  const doc = revision.document as unknown as WorkflowDoc;
  const start = useStartWorkflowRun(workflowId);
  const [params, setParams] = useState<JsonObject>({});
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [useCache, setUseCache] = useState(true);
  const [submitted, setSubmitted] = useState(false);
  const key = useMemo(() => newIdempotencyKey(), []);
  const missing = [...missingInputs(doc.inputs ?? {}, inputs), ...missingParams(doc.params ?? {}, params)];
  const invalid = !revision.validation.valid;
  const problemErrors = start.error instanceof ApiError ? (start.error.problem?.errors ?? []) : [];
  const fieldErrors = Object.fromEntries(problemErrors.filter((i) => i.pointer?.startsWith("/params/")).map((i) => [i.pointer!.split("/")[2] ?? "", i.message]));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (missing.length || invalid) return;
    start.mutate(
      { body: { revision: revision.revision, params, inputs, use_cache: useCache }, key },
      { onSuccess: ({ data }) => void navigate({ to: "/workflow-runs/$runId", params: { runId: data.run_id } }) },
    );
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>Run revision {revision.revision}</DialogTitle>
        <DialogDescription>Choose the data and adjust the parameters for this run. The document itself does not change.</DialogDescription>
        {invalid ? (
          <div role="alert" className="grid gap-2 rounded-md border border-destructive/35 bg-destructive/6 p-3">
            <p className="text-[13px] font-medium">This revision has problems and cannot run. Fix them in the editor and save a new revision.</p>
            <IssueList issues={(revision.validation.issues ?? []).filter((i) => (i.severity ?? "error") === "error")} />
          </div>
        ) : null}
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <section className="grid gap-3" aria-label="Inputs">
            <h3 className="text-sm font-semibold">Data</h3>
            <InputsForm inputs={doc.inputs ?? {}} value={inputs} onChange={setInputs} />
          </section>
          <section className="grid gap-3" aria-label="Parameters">
            <h3 className="text-sm font-semibold">Parameters</h3>
            <ParamsForm doc={doc} value={params} onChange={setParams} errors={fieldErrors} />
          </section>
          <label className="flex items-center gap-2 text-[13px]">
            <Checkbox checked={useCache} onCheckedChange={(c) => setUseCache(c === true)} />
            Reuse results of steps whose inputs did not change (faster and cheaper)
          </label>
          {submitted && missing.length ? (
            <p role="alert" className="text-[13px] text-destructive">
              Still needed: {missing.join(", ")}.
            </p>
          ) : null}
          {start.isError ? <ErrorNotice error={start.error} title="The run did not start" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={start.isPending || invalid}>
              {start.isPending ? <Spinner /> : <Play />} Start run
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
