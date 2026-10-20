import { useNavigate } from "@tanstack/react-router";
import { Ban, Play, RotateCcw } from "lucide-react";
import { useState } from "react";

import { errorMessage, newIdempotencyKey } from "@/api/client";
import { can } from "@/api/access";
import { ErrorNotice } from "@/components/common";
import { useGrants } from "@/features/auth/workspace";
import { useCancelRun, useResumeRun, useRetryRun } from "@/api/queries";
import type { ResourceClass, Run } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { GPU_CLASS, GPU_CLASS_HELP, isCancellable, nextResourceClass, resourceClassLabel, RETRY_CLASSES } from "@/lib/run-state";
import { toast } from "@/stores/toast";

export function RunActions({ run }: { run: Run }) {
  const cancel = useCancelRun();
  const resume = useResumeRun();
  const grants = useGrants();
  const canCancel = can(grants, "cancel", "run");
  const canRun = can(grants, "create", "run");
  const needs = (action: "cancel" | "create") => (grants.projectRole ? `You need the runner role. Your role is ${grants.projectRole}.` : `You cannot ${action} runs.`);
  const [retryOpen, setRetryOpen] = useState(false);
  return (
    <>
      {isCancellable(run.state) ? (
        <Button
          variant="outline"
          disabled={cancel.isPending || !canCancel}
          title={canCancel ? undefined : needs("cancel")}
          onClick={() => {
            if (!window.confirm("Cancel this run? Fits with checkpoints stop as partial. You can resume them.")) return;
            cancel.mutate(run.id, {
              onSuccess: () => toast.info("The app sent the cancel request"),
              onError: (e) => toast.error("Cannot cancel the run", errorMessage(e)),
            });
          }}
        >
          <Ban /> Cancel
        </Button>
      ) : null}
      {run.state === "partial" ? (
        <Button
          disabled={resume.isPending || !canRun}
          title={canRun ? undefined : needs("create")}
          onClick={() =>
            resume.mutate(run.id, {
              onSuccess: () => toast.success("Run resumed from its checkpoints"),
              onError: (e) => toast.error("Cannot resume the run", errorMessage(e)),
            })
          }
        >
          <Play /> Resume
        </Button>
      ) : null}
      {run.state === "failed" ? (
        <Button
          variant={run.failure_class === "resource_exhausted" ? "default" : "outline"}
          disabled={!canRun}
          title={canRun ? undefined : needs("create")}
          onClick={() => setRetryOpen(true)}
        >
          <RotateCcw /> Retry
        </Button>
      ) : null}
      {retryOpen ? <RetryDialog run={run} onClose={() => setRetryOpen(false)} /> : null}
    </>
  );
}

function RetryDialog({ run, onClose }: { run: Run; onClose: () => void }) {
  const retry = useRetryRun();
  const navigate = useNavigate();
  const suggested = run.failure_class === "resource_exhausted" ? nextResourceClass(run.resource_class) : null;
  const [rc, setRc] = useState<ResourceClass>(suggested ?? run.resource_class);
  const [seed, setSeed] = useState("");
  const [key] = useState(newIdempotencyKey);
  const seedValue = seed.trim() === "" ? null : Number(seed);
  const seedValid = seedValue === null || (Number.isInteger(seedValue) && seedValue >= 0 && seedValue < 2 ** 32);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Retry as a new run</DialogTitle>
        <DialogDescription>
          {suggested
            ? `The run did not have sufficient resources at class ${run.resource_class}. A retry at ${suggested} usually solves this problem.`
            : "The app makes a new run from the same inputs. This run does not change."}
        </DialogDescription>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Resource class" htmlFor="retry-rc" hint={(rc as string) === GPU_CLASS ? GPU_CLASS_HELP : undefined}>
            <NativeSelect
              id="retry-rc"
              value={rc}
              onChange={(e) => {
                setRc(e.target.value as ResourceClass);
                retry.reset();
              }}
            >
              {RETRY_CLASSES.map((c) => (
                <option key={c} value={c} title={c === GPU_CLASS ? GPU_CLASS_HELP : undefined}>
                  {resourceClassLabel(c)}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Seed (optional)" htmlFor="retry-seed" hint={seedValid ? undefined : "The seed is not correct. Type a whole number less than 2³²."}>
            <Input id="retry-seed" inputMode="numeric" value={seed} onChange={(e) => setSeed(e.target.value)} aria-invalid={!seedValid} />
          </Field>
        </div>
        {retry.isError ? (
          <ErrorNotice error={retry.error} title="Cannot start the retry" onRetry={() => retry.reset()} />
        ) : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep as is
          </Button>
          <Button
            disabled={!seedValid || retry.isPending}
            onClick={() =>
              retry.mutate(
                {
                  id: run.id,
                  key,
                  body: {
                    resource_class: rc,
                    ...(seedValue !== null ? { seed: seedValue } : {}),
                    ...(run.problem?.code ? { reason: `${run.problem.code} at ${run.resource_class}` } : {}),
                  },
                },
                {
                  onSuccess: (next) => {
                    toast.success("Retry started");
                    onClose();
                    void navigate({ to: "/runs/$runId", params: { runId: next.id } });
                  },
                },
              )
            }
          >
            Start retry
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
