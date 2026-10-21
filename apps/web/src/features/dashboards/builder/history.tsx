/**
 * Builder › History: what waits for the analyst and everything the dashboard produced.
 *
 * - Waiting for review: uploads and kept tried cases (`pending_review`) to publish into the
 *   clients' history or hide (plan §10.2). A tried case shows its inputs and its prediction.
 * - Refit requests (§10.4), next to them: approve (publishes the readout, adds the numbers as
 *   a new dataset version and starts the refit workflow as you; needs the runner role) or
 *   reject (nothing runs).
 * - All readouts (failed and private ones included, which clients never see), "Refresh now"
 *   and the published revisions.
 */
import { Link } from "@tanstack/react-router";
import { Bookmark, Check, ChevronDown, ChevronRight, EyeOff, RefreshCw, Send, X } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import { EmptyState, ErrorNotice, Mono, QueryView, Spinner } from "@/components/common";
import { InlineMessage, TagWord } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { useWorkflows } from "@/features/workflows/api";
import { absoluteTime, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { useAnalystReadouts, useApproveRefit, useDashboardRevisions, useHideReadout, useKeepTriedCase, useMemberClient, usePublishReadout, useRefitRequests, useRefreshDashboard, useRejectRefit } from "../api";
import { dashboardErrorMessage, toClientDashboard, useReadout } from "../client";
import { inputLabels, PredictionView } from "../readout/panels/results";
import { Section, Working } from "../readout/parts";
import { historyRow, KIND_WORD } from "../readout/readout-model";
import type { Dashboard, DashboardRefitRequest, ReadoutHeader } from "../types";
import { refitApprovePermission } from "./spec-form";

const REQUESTER_WORD: Record<DashboardRefitRequest["requested_by_kind"], string> = { member: "a team member", client: "a client user", share: "a share link" };
const REFIT_STATUS: Record<DashboardRefitRequest["status"], { label: string; tone: "info" | "positive" | "neutral" | "negative" }> = {
  open: { label: "Open", tone: "info" },
  approved: { label: "Approved", tone: "positive" },
  rejected: { label: "Rejected", tone: "neutral" },
  superseded: { label: "Replaced by a newer request", tone: "neutral" },
  failed: { label: "Failed", tone: "negative" },
};

export function HistoryTab({ dashboard }: { dashboard: Dashboard }) {
  const ws = useWorkspace();
  const canReview = can(ws.grants, "update", "dashboard");
  const refresh = useRefreshDashboard(dashboard.id);
  const published = dashboard.status === "published";
  return (
    <div className="grid min-w-0 gap-5">
      <div className="grid min-w-0 gap-5 xl:grid-cols-2">
        <PendingSection dashboard={dashboard} canReview={canReview} />
        <RefitSection dashboard={dashboard} />
      </div>
      <Section
        title="All readouts"
        description="Clients see only the published, ready readouts. Failed refreshes, private scenarios and hidden readouts show here only."
        action={
          <Button
            size="sm"
            variant="secondary"
            disabled={!canReview || !published || refresh.isPending}
            title={!published ? "Publish the dashboard first." : undefined}
            onClick={() =>
              refresh.mutate(undefined, {
                onSuccess: (h) => toast.success("Refresh started", `Readout ${h.seq} appears here when it is ready.`),
                onError: (e) => toast.error("The refresh did not start", errorMessage(e)),
              })
            }
          >
            {refresh.isPending ? <Spinner /> : <RefreshCw />} Refresh now
          </Button>
        }
      >
        <ReadoutTable dashboard={dashboard} canReview={canReview} />
      </Section>
      <RevisionsSection dashboard={dashboard} />
    </div>
  );
}

function Tags({ h }: { h: ReadoutHeader }) {
  const row = historyRow(h);
  return (
    <span className="flex flex-wrap gap-1">
      {h.visibility === "published" && h.status === "ready" ? <TagWord tone="positive">Published</TagWord> : null}
      {row.tags.map((t) => (
        <TagWord key={t.label} tone={t.tone}>
          {t.label}
        </TagWord>
      ))}
    </span>
  );
}

/** Keep somebody's private tried case (an editor): it stops expiring and waits for review. */
function KeepButton({ dashboardId, h, canReview }: { dashboardId: string; h: ReadoutHeader; canReview: boolean }) {
  const keep = useKeepTriedCase(dashboardId);
  return (
    <Button
      size="sm"
      variant="secondary"
      disabled={!canReview || keep.isPending || h.status !== "ready"}
      title={h.status !== "ready" ? "The readout is not ready yet." : "It stops expiring and waits for review, so you can publish it."}
      aria-label={`Keep tried case ${h.seq}`}
      onClick={() =>
        keep.mutate(h.seq, {
          onSuccess: () => toast.success("Tried case kept", `${h.title} waits for review.`),
          onError: (e) => toast.error("The tried case was not kept", errorMessage(e)),
        })
      }
    >
      <Bookmark /> Keep
    </Button>
  );
}

function ReviewButtons({ dashboardId, h, canReview }: { dashboardId: string; h: ReadoutHeader; canReview: boolean }) {
  const publish = usePublishReadout(dashboardId);
  const hide = useHideReadout(dashboardId);
  if (h.kind === "prediction" && h.visibility === "private") return <KeepButton dashboardId={dashboardId} h={h} canReview={canReview} />;
  if (h.kind === "scenario" || h.visibility === "private") return null;
  const busy = publish.isPending || hide.isPending;
  return (
    <span className="flex flex-wrap justify-end gap-1">
      {h.visibility !== "published" && h.status !== "failed" ? (
        <Button
          size="sm"
          variant="secondary"
          disabled={!canReview || busy || h.status !== "ready"}
          title={h.status !== "ready" ? "The readout is not ready yet." : undefined}
          aria-label={`Publish readout ${h.seq}`}
          onClick={() =>
            publish.mutate(h.seq, {
              onSuccess: () => toast.success("Readout published", `${h.title} is in the clients' history.`),
              onError: (e) => toast.error("The readout was not published", errorMessage(e)),
            })
          }
        >
          <Send /> Publish
        </Button>
      ) : null}
      {h.visibility !== "hidden" ? (
        <Button
          size="sm"
          variant="ghost"
          disabled={!canReview || busy}
          aria-label={`Hide readout ${h.seq}`}
          onClick={() =>
            hide.mutate(h.seq, {
              onSuccess: () => toast.success("Readout hidden", `Clients do not see ${h.title}.`),
              onError: (e) => toast.error("The readout was not hidden", errorMessage(e)),
            })
          }
        >
          <EyeOff /> Hide
        </Button>
      ) : null}
    </span>
  );
}

/** A kept tried case: the values the client tried and what the model predicted. */
function TriedCase({ dashboard, seq }: { dashboard: Dashboard; seq: number }) {
  const client = useMemberClient(dashboard.id, { spec: "published" });
  const q = useReadout(client, seq);
  if (q.isError) return <InlineMessage tone="error">{dashboardErrorMessage(q.error)}</InlineMessage>;
  if (!q.data) return <Working>Loading the tried case…</Working>;
  const view = toClientDashboard(dashboard, null, dashboard.published_revision ? "published" : "draft");
  const prediction = q.data.snapshot?.prediction;
  return prediction ? <PredictionView dashboard={view} result={prediction} labels={inputLabels(view)} /> : <p className="text-sm text-muted-foreground">{q.data.snapshot?.lead ?? "No prediction to show."}</p>;
}

function PendingItem({ dashboard, h, canReview }: { dashboard: Dashboard; h: ReadoutHeader; canReview: boolean }) {
  const [open, setOpen] = useState(false);
  const tried = h.kind === "prediction";
  return (
    <li data-readout={h.seq} data-kind={h.kind} className="grid gap-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <span className="grid min-w-0 gap-1">
          <span className="font-medium [overflow-wrap:anywhere]">{h.title}</span>
          <span className="text-xs text-muted-foreground">
            {tried ? "Tried case" : KIND_WORD[h.kind]} · {tried ? "kept" : "sent"} {relativeTime(h.created_at)}
          </span>
          <Tags h={h} />
        </span>
        <ReviewButtons dashboardId={dashboard.id} h={h} canReview={canReview} />
      </div>
      {tried && h.status === "ready" ? (
        <div className="grid gap-2">
          <div>
            <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen(!open)}>
              {open ? <ChevronDown /> : <ChevronRight />} {open ? "Hide the case" : "Show the case"}
            </Button>
          </div>
          {open ? <TriedCase dashboard={dashboard} seq={h.seq} /> : null}
        </div>
      ) : null}
    </li>
  );
}

function PendingSection({ dashboard, canReview }: { dashboard: Dashboard; canReview: boolean }) {
  const pending = useAnalystReadouts(dashboard.id, { visibility: "pending_review" }, { poll: true });
  return (
    <Section
      title="Waiting for review"
      description="Uploads that clients and share links sent, and tried cases that clients kept. The sender already sees the result. Publish it into everyone's history, or hide it."
    >
      <QueryView query={pending}>
        {(page) => {
          const items = (page.items ?? []).filter((h) => h.status !== "failed");
          return items.length ? (
            <ul className="grid gap-3" aria-label="Readouts waiting for review">
              {items.map((h) => (
                <PendingItem key={h.seq} dashboard={dashboard} h={h} canReview={canReview} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">Nothing waits for review.</p>
          );
        }}
      </QueryView>
    </Section>
  );
}

// ------------------------------------------------------------------------------ refit requests

function RefitSection({ dashboard }: { dashboard: Dashboard }) {
  const ws = useWorkspace();
  const requests = useRefitRequests(dashboard.id);
  const workflows = useWorkflows();
  const canDecide = can(ws.grants, "update", "dashboard");
  const approve = refitApprovePermission(ws.grants.projectRole);
  const [deciding, setDeciding] = useState<{ r: DashboardRefitRequest; action: "approve" | "reject" } | null>(null);
  const spec = dashboard.published_revision?.spec ?? dashboard.draft_spec;
  const wfId = spec.actuals?.refit_workflow_id ?? null;
  const wfName = wfId ? (workflows.data?.items.find((w) => w.id === wfId)?.name ?? wfId) : null;
  return (
    <Section
      title="Refit requests"
      description={
        wfName ? (
          <>
            Uploaders can ask you to refit the model with their numbers. Approving starts{" "}
            <Link to="/workflows/$workflowId" params={{ workflowId: wfId ?? "" }} className="text-primary hover:underline">
              {wfName}
            </Link>{" "}
            as you. Nothing runs before you approve.
          </>
        ) : (
          "Uploaders can ask you to refit the model with their numbers when you choose a refit workflow on the Inputs tab."
        )
      }
    >
      {!approve.allowed && canDecide ? (
        <InlineMessage tone="info" size="s" role="none">
          {approve.reason}
        </InlineMessage>
      ) : null}
      <QueryView query={requests}>
        {(page) => {
          const items = [...(page.items ?? [])].sort((a, b) => (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1) || b.created_at.localeCompare(a.created_at));
          return items.length ? (
            <ul className="grid gap-3" aria-label="Refit requests">
              {items.map((r) => (
                <li key={r.id} data-refit={r.id} data-status={r.status} className="grid gap-2 rounded-lg border p-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <span className="grid min-w-0 gap-1">
                      <span className="font-medium">Refit with the numbers of readout {r.readout_seq}</span>
                      <span className="text-xs text-muted-foreground">
                        Asked by {REQUESTER_WORD[r.requested_by_kind]} {relativeTime(r.created_at)}
                        {r.decided_at ? ` · decided ${relativeTime(r.decided_at)}` : ""}
                      </span>
                      <span className="flex flex-wrap gap-1">
                        <TagWord tone={REFIT_STATUS[r.status].tone}>{REFIT_STATUS[r.status].label}</TagWord>
                      </span>
                    </span>
                    {r.status === "open" ? (
                      <span className="flex flex-wrap justify-end gap-1">
                        <Button
                          size="sm"
                          disabled={!approve.allowed}
                          title={approve.reason ?? undefined}
                          aria-describedby={approve.allowed ? undefined : `refit-why-${r.id}`}
                          onClick={() => setDeciding({ r, action: "approve" })}
                        >
                          <Check /> Approve
                        </Button>
                        <Button size="sm" variant="ghost" disabled={!canDecide} onClick={() => setDeciding({ r, action: "reject" })}>
                          <X /> Reject
                        </Button>
                      </span>
                    ) : null}
                  </div>
                  {r.status === "open" && !approve.allowed ? (
                    <p id={`refit-why-${r.id}`} className="text-xs text-muted-foreground">
                      {approve.reason}
                    </p>
                  ) : null}
                  {r.note ? <p className="text-sm text-muted-foreground [overflow-wrap:anywhere]">Note: {r.note}</p> : null}
                  {r.workflow_run_id ? (
                    <Link to="/workflow-runs/$runId" params={{ runId: r.workflow_run_id }} className="text-sm text-primary hover:underline">
                      Open the refit run <Mono>{r.workflow_run_id}</Mono>
                    </Link>
                  ) : null}
                  {r.problem ? <ErrorNotice error={new Error(r.problem.detail ?? r.problem.title)} title="The refit did not start" /> : null}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No refit requests">{spec.actuals?.allow_refit_requests ? "When an uploader asks for a refit, the request shows here." : "Refit requests are off for this dashboard."}</EmptyState>
          );
        }}
      </QueryView>
      {deciding ? <DecideRefitDialog dashboardId={dashboard.id} request={deciding.r} action={deciding.action} workflow={wfName} onClose={() => setDeciding(null)} /> : null}
    </Section>
  );
}

function DecideRefitDialog({ dashboardId, request, action, workflow, onClose }: { dashboardId: string; request: DashboardRefitRequest; action: "approve" | "reject"; workflow: string | null; onClose: () => void }) {
  const approve = useApproveRefit(dashboardId);
  const reject = useRejectRefit(dashboardId);
  const m = action === "approve" ? approve : reject;
  const [note, setNote] = useState("");
  const [idemKey] = useState(() => crypto.randomUUID());
  const submit = () => {
    const body = { requestId: request.id, note: note.trim() || null };
    const done = {
      onSuccess: () => {
        toast.success(action === "approve" ? "Refit approved" : "Refit rejected", action === "approve" ? "The refit run started. A new readout appears when it succeeds." : "Nothing runs. The comparison stays available.");
        onClose();
      },
    };
    if (action === "approve") approve.mutate({ ...body, idemKey }, done);
    else reject.mutate(body, done);
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>{action === "approve" ? "Approve the refit?" : "Reject the refit?"}</DialogTitle>
        <DialogDescription>
          {action === "approve"
            ? `The app publishes readout ${request.readout_seq}, adds the uploaded numbers as a new version of the dashboard's dataset, and starts ${workflow ?? "the refit workflow"} as you. The run uses your project's compute, quotas and approvals.`
            : `Nothing runs. Readout ${request.readout_seq} stays waiting for review, and the uploader sees “Refit not done”.`}
        </DialogDescription>
        <Field label="Note (optional)" htmlFor="refit-note" hint="The uploader does not see the note. Your team does.">
          <Textarea id="refit-note" maxLength={400} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        {m.isError ? <ErrorNotice error={m.error} title={action === "approve" ? "The refit was not approved" : "The request was not rejected"} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={action === "approve" ? "default" : "destructive"} disabled={m.isPending} onClick={submit}>
            {m.isPending ? <Spinner /> : action === "approve" ? <Check /> : <X />} {action === "approve" ? "Approve and start the refit" : "Reject"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------------ all readouts and revisions

function ReadoutTable({ dashboard, canReview }: { dashboard: Dashboard; canReview: boolean }) {
  const readouts = useAnalystReadouts(dashboard.id, {}, { poll: true });
  return (
    <QueryView query={readouts}>
      {(page) =>
        page.items?.length ? (
          <div className="min-w-0 overflow-x-auto">
            <Table aria-label="All readouts">
              <THead>
                <TR>
                  <TH>#</TH>
                  <TH>Readout</TH>
                  <TH>Kind</TH>
                  <TH>State</TH>
                  <TH>Made</TH>
                  <TH>
                    <span className="sr-only">Actions</span>
                  </TH>
                </TR>
              </THead>
              <TBody>
                {page.items.map((h) => (
                  <TR key={h.seq} data-readout={h.seq} data-visibility={h.visibility}>
                    <TD className="tabular">{h.seq}</TD>
                    <TD className="min-w-48 font-medium [overflow-wrap:anywhere]">{h.title}</TD>
                    <TD className="text-sm">{KIND_WORD[h.kind]}</TD>
                    <TD>
                      <Tags h={h} />
                    </TD>
                    <TD title={absoluteTime(h.created_at)}>{relativeTime(h.created_at)}</TD>
                    <TD className="text-right">{h.visibility === "pending_review" ? null : <ReviewButtons dashboardId={dashboard.id} h={h} canReview={canReview} />}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{dashboard.status === "published" ? "The first readout is on its way." : "Readouts start when you publish the dashboard."}</p>
        )
      }
    </QueryView>
  );
}

function RevisionsSection({ dashboard }: { dashboard: Dashboard }) {
  const revisions = useDashboardRevisions(dashboard.id);
  return (
    <Section title="Published revisions" description="Each publish pins the spec and the model version. Readouts name the version that made them.">
      <QueryView query={revisions}>
        {(page) =>
          page.items.length ? (
            <div className="min-w-0 overflow-x-auto">
              <Table aria-label="Published revisions">
                <THead>
                  <TR>
                    <TH>Revision</TH>
                    <TH>Model</TH>
                    <TH>Published</TH>
                  </TR>
                </THead>
                <TBody>
                  {page.items.map((r) => (
                    <TR key={r.id}>
                      <TD className="tabular">{r.seq}</TD>
                      <TD>
                        <Mono className="[overflow-wrap:anywhere]">{r.model_ref}</Mono>
                        {r.endpoint_alias ? <span className="block text-xs text-muted-foreground">through {r.endpoint_alias}</span> : null}
                      </TD>
                      <TD title={absoluteTime(r.created_at)}>{relativeTime(r.created_at)}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">The dashboard was not published yet.</p>
          )
        }
      </QueryView>
    </Section>
  );
}
