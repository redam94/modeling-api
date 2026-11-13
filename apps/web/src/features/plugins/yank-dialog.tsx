/**
 * Withdraw a version (D15). Before anything happens the dialog shows what uses it
 * (`GET …/usage`): runs in the last 30 days, model versions, endpoints and dashboards. A
 * security withdrawal also cancels queued and running runs that use it.
 */
import { useState } from "react";

import { errorMessage } from "@/api/client";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, NativeSelect, Textarea } from "@/components/ui/primitives";
import { toast } from "@/stores/toast";

import { usePluginUsage, useYank } from "./api";
import { displayText } from "./states";
import type { PluginVersion, YankChoice } from "./types";
import { NOTE_MAX, YANK_CHOICES } from "./types";

const REASON_LABEL: Record<YankChoice, string> = {
  withdrawn: "I don't want it used any more",
  superseded: "A newer version replaces it",
  security: "It is unsafe (also stops runs that use it now)",
};

function Count({ n, word, testid }: { n: number; word: string; testid?: string }) {
  return (
    <div className="grid gap-0.5 rounded-xl bg-muted px-4 py-3" data-testid={testid}>
      <dt className="text-xs text-muted-foreground">{word}</dt>
      <dd className="font-display text-xl font-semibold tabular">{n.toLocaleString()}</dd>
    </div>
  );
}

export function YankDialog({ version, open, onOpenChange }: { version: PluginVersion; open: boolean; onOpenChange: (open: boolean) => void }) {
  const usage = usePluginUsage(version.plugin_id, version.id, open);
  const yank = useYank();
  const [reason, setReason] = useState<YankChoice>("withdrawn");
  const [note, setNote] = useState("");
  const u = usage.data;
  const truncated = u?.truncated ? "+" : "";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>Withdraw {displayText(version.version)}?</DialogTitle>
        <DialogDescription>
          New runs can't use it after this, and it leaves the catalog for everyone at once. Results that already exist stay readable, but anything that needs the code again (scoring, refits,
          endpoints, dashboard refreshes) stops working. You can't undo this.
        </DialogDescription>
        <div data-testid="plugin-usage" className="grid gap-2">
          <p className="text-sm font-semibold">What uses it now</p>
          {usage.isPending ? (
            <LoadingRows rows={2} />
          ) : usage.isError ? (
            <ErrorNotice error={usage.error} title="The app cannot load what uses this version" onRetry={() => void usage.refetch()} />
          ) : u ? (
            <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Count n={u.runs_30d ?? 0} word="Runs, last 30 days" testid="plugin-usage-runs" />
              <Count n={u.model_versions?.length ?? 0} word={`Model versions${truncated}`} />
              <Count n={u.endpoints?.length ?? 0} word={`Endpoints${truncated}`} />
              <Count n={u.dashboards?.length ?? 0} word={`Dashboards${truncated}`} />
            </dl>
          ) : null}
        </div>
        <Field label="Why" htmlFor="yank-reason">
          <NativeSelect id="yank-reason" value={reason} onChange={(e) => setReason(e.target.value as YankChoice)}>
            {YANK_CHOICES.map((r) => (
              <option key={r} value={r}>
                {REASON_LABEL[r]}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field label="Note (optional)" htmlFor="yank-note" hint="The uploader and the workspace admins see it.">
          <Textarea id="yank-note" value={note} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            data-testid="plugin-yank-confirm"
            disabled={yank.isPending}
            onClick={() =>
              yank.mutate(
                { pluginId: version.plugin_id, versionId: version.id, body: { reason, note } },
                {
                  onSuccess: () => {
                    toast.success("Version withdrawn", `${version.version} can't be used by new runs.`);
                    onOpenChange(false);
                  },
                  onError: (err) => toast.error("The app could not withdraw the version", errorMessage(err)),
                },
              )
            }
          >
            Withdraw {displayText(version.version)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
