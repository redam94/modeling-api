/**
 * Publish: check the saved draft (`:validate`), say what publishing does, then publish. The
 * revision pins the model version (an endpoint resolves to its current version), live inputs
 * run as the publisher, and the first readout is queued. Publishing needs the runner role.
 */
import { Send } from "lucide-react";

import { ApiError } from "@/api/client";
import type { Issue } from "@/api/types";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { useWorkspace } from "@/features/auth/workspace";
import { emitOnboarding } from "@/lib/onboarding-events";
import { toast } from "@/stores/toast";

import { usePublishDashboard, useValidateDashboard } from "../api";
import { isLayout2, type AnyDashboardSpec, type Dashboard } from "../types";
import { publishPermission } from "./spec-form";
import { startsRuns } from "./spec-form-v2";

function publishStale(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 412 || e.code === "DASHBOARD_SPEC_STALE");
}

/** The sentence the publisher accepts (plan §3.6.3). */
export const PUBLISH_COST = "Live inputs run as you and use your project's compute.";

/** Does the draft have inputs that start runs (either layout)? */
export function hasLiveInputs(spec: AnyDashboardSpec): boolean {
  return isLayout2(spec) ? startsRuns(spec) : (spec.whatif?.exact ?? false) || (spec.actuals?.enabled ?? false);
}

/** "Up to 20 runs each day." (plus the scored files of a layout 2 draft). */
export function runsWords(spec: AnyDashboardSpec): string {
  const runs = spec.limits?.exact_runs_per_day ?? 20;
  const files = isLayout2(spec) && spec.batch?.enabled ? ` Up to ${spec.limits?.batch_runs_per_day ?? 3} of them can score files.` : "";
  return `Up to ${runs} runs each day.${files}`;
}

export function PublishDialog({ dashboard, onClose, onIssue }: { dashboard: Dashboard; onClose: () => void; onIssue?: (issue: Issue) => void }) {
  const ws = useWorkspace();
  const perm = publishPermission(ws.grants.projectRole);
  const check = useValidateDashboard(dashboard.id, dashboard.draft_etag);
  const publish = usePublishDashboard(dashboard.id);
  const spec = dashboard.draft_spec;
  const live = hasLiveInputs(spec);
  const firstV2 = isLayout2(spec) && dashboard.published_revision != null && !isLayout2(dashboard.published_revision.spec);
  const again = dashboard.status === "published";
  const issues = check.data?.issues ?? [];
  const errors = issues.filter((i) => i.severity !== "warning" && i.severity !== "info");
  const ok = check.isSuccess && check.data.ok;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>{again ? "Publish the changes?" : `Publish “${spec.title}”?`}</DialogTitle>
        <DialogDescription>
          {again
            ? "Clients and share links see the new revision from their next page load. The history stays."
            : "Clients with access and share links can see the dashboard. The app makes the first readout now; it takes a few minutes."}{" "}
          Publishing pins the model version{spec.source.kind === "endpoint" ? ` that ${spec.source.alias ?? "the endpoint"} serves now` : ""}.
        </DialogDescription>
        <div data-testid="publish-cost">
          <InlineMessage tone={live ? "warning" : "info"} role="none">
            {PUBLISH_COST}
            {live ? ` ${runsWords(spec)}` : " This dashboard has no inputs that start runs now."}
          </InlineMessage>
        </div>
        {firstV2 ? (
          <InlineMessage tone="info" size="s" role="none">
            Clients move to the new layout. They see a short note that the numbers come from the same model. Their likely ranges now show where one new result will probably be, so they can be wider than before.
            The old readouts stay in the history.
          </InlineMessage>
        ) : null}
        {check.isPending ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> Checking the draft…
          </p>
        ) : check.isError ? (
          <ErrorNotice error={check.error} title="The app could not check the draft" onRetry={() => void check.refetch()} />
        ) : errors.length ? (
          <div className="grid gap-2">
            <p className="text-sm font-medium">Fix these first:</p>
            <IssueList
              issues={errors}
              onSelect={(i) => {
                onIssue?.(i);
                onClose();
              }}
            />
          </div>
        ) : (
          <InlineMessage tone="success" size="s" role="none">
            The draft is ready to publish.
          </InlineMessage>
        )}
        {!perm.allowed ? (
          <InlineMessage tone="warning" size="s" role="none">
            {perm.reason}
          </InlineMessage>
        ) : null}
        {publish.isError ? (
          <ErrorNotice
            error={publish.error}
            title={publishStale(publish.error) ? "Another person changed the draft" : "The dashboard was not published"}
          />
        ) : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!perm.allowed || !ok || publish.isPending}
            onClick={() =>
              publish.mutate(dashboard.draft_etag, {
                onSuccess: (d) => {
                  // v0.18 D25: publishing also ticks "Make a dashboard".
                  emitOnboarding("dashboard_published", { dashboard: d.id });
                  toast.success(again ? "Changes published" : "Dashboard published", `Revision ${d.published_revision?.seq ?? 1} is live. The new readout appears on the History tab when it is ready.`);
                  onClose();
                },
              })
            }
          >
            {publish.isPending ? <Spinner /> : <Send />} {again ? "Publish changes" : "Publish"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
