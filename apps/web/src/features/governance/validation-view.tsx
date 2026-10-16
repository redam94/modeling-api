/**
 * One validation of a model version: where it is in the status state machine and how it got
 * there, every check's result with its evidence, the reviewers' decisions (approve or reject with
 * a comment; never the submitter, hidden without the reviewer capability), and the actions its
 * state allows: cancel, resubmit, deprecate and export the evidence pack.
 */
import { Link } from "@tanstack/react-router";
import { Ban, CheckCircle2, Download, FileCheck2, RotateCcw, ShieldOff, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { useModel } from "@/api/queries";
import { ErrorNotice, KeyValues, LoadingRows, Mono, PageHeader, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, NativeSelect, Textarea } from "@/components/ui/primitives";
import { PersonName } from "@/features/auth/people";
import { useWorkspace } from "@/features/auth/workspace";
import { fetchArtifactBytes } from "@/lib/artifacts";
import { saveBytes } from "@/lib/download";
import { absoluteTime, relativeTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { useGovernanceChecks, usePolicies, useReviewer, useValidation, useValidationActions } from "./api";
import { CheckResultList } from "./evidence";
import { EVENT_LABEL, MAIN_PATH, TONE_BADGE, VALIDATION_META } from "./status";
import type { ModelValidation, ValidationStatus } from "./types";

export function ValidationBadge({ status }: { status: ValidationStatus }) {
  const meta = VALIDATION_META[status];
  return (
    <Badge variant={TONE_BADGE[meta.tone]} title={meta.plain} data-validation-status={status}>
      {meta.label}
    </Badge>
  );
}

/** The happy path with the current state marked; side exits (failed, rejected, deprecated) beside it. */
export function StatusTimeline({ validation }: { validation: ModelValidation }) {
  const status = validation.status;
  const visited = new Set<ValidationStatus>([...(validation.history ?? []).flatMap((h) => [h.from_status, h.to_status]), status]);
  const offPath = !MAIN_PATH.includes(status);
  const path = validation.require_approval ? MAIN_PATH : MAIN_PATH.filter((s) => s !== "awaiting_approval");
  return (
    <div className="grid gap-4">
      <ol className="flex flex-wrap items-center gap-1" aria-label="Validation progress">
        {path.map((s, i) => {
          const current = s === status;
          const done = visited.has(s) && !current && path.indexOf(status) > i;
          return (
            <li key={s} className="flex items-center gap-1">
              <span
                aria-current={current ? "step" : undefined}
                className={cn(
                  "rounded-full border px-2.5 py-1 text-xs font-medium",
                  current && "border-primary bg-accent text-accent-foreground",
                  done && "border-success/40 text-success",
                  !current && !done && "text-muted-foreground",
                )}
              >
                {done ? <CheckCircle2 className="mr-1 inline size-3" aria-hidden /> : null}
                {VALIDATION_META[s].label}
              </span>
              {i < path.length - 1 ? <span aria-hidden className="text-muted-foreground">→</span> : null}
            </li>
          );
        })}
        {offPath ? (
          <li className="ml-2">
            <span aria-current="step" className="rounded-full border border-destructive/40 bg-destructive/8 px-2.5 py-1 text-xs font-medium text-destructive">
              {VALIDATION_META[status].label}
            </span>
          </li>
        ) : null}
      </ol>
      <p className="text-[13px] text-muted-foreground">{VALIDATION_META[status].plain}</p>
      {(validation.history ?? []).length ? (
        <ol className="grid gap-1.5 border-l pl-4" aria-label="History">
          {[...(validation.history ?? [])].reverse().map((h, i) => (
            <li key={`${h.at}-${i}`} className="relative text-[13px]">
              <span aria-hidden className="absolute top-1.5 -left-[21px] size-2 rounded-full bg-border" />
              <span className="font-medium">{EVENT_LABEL[h.event]}</span>
              <span className="text-muted-foreground">
                {" "}
                · {VALIDATION_META[h.from_status].label} → {VALIDATION_META[h.to_status].label} · {h.actor_id === "system" ? "the platform" : h.actor_id ? <PersonName id={h.actor_id} /> : "—"} ·{" "}
                <time dateTime={h.at} title={h.at}>
                  {relativeTime(h.at)}
                </time>
              </span>
              {h.note ? <span className="block text-muted-foreground">“{h.note}”</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

function DecisionPanel({ validation }: { validation: ModelValidation }) {
  const reviewer = useReviewer();
  const { decide } = useValidationActions(validation.id);
  const [comment, setComment] = useState("");
  const [tried, setTried] = useState<"approve" | "reject" | null>(null);
  const approvals = validation.approvals ?? [];
  const isSubmitter = Boolean(validation.submitted_by && validation.submitted_by === reviewer.userId);
  const canDecide = validation.status === "awaiting_approval";
  const rejectNeedsComment = tried === "reject" && !comment.trim();
  const send = (decision: "approve" | "reject") => {
    setTried(decision);
    if (decision === "reject" && !comment.trim()) return;
    decide.mutate(
      { decision, comment: comment.trim() || null },
      {
        onSuccess: (v) => {
          toast.success(decision === "approve" ? "Approved" : "Rejected", VALIDATION_META[v.status].plain);
          setComment("");
          setTried(null);
        },
      },
    );
  };
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Approvals</CardTitle>
          <CardDescription>
            {validation.require_approval ? `${validation.approvals_required ?? 1} reviewer approval${(validation.approvals_required ?? 1) === 1 ? "" : "s"} needed, from someone other than the submitter.` : "This validation needs no approval."}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        {approvals.length ? (
          <ul className="grid gap-2">
            {approvals.map((a) => (
              <li key={a.id} className="flex items-start gap-2 text-[13px]">
                {a.decision === "approve" ? <ThumbsUp className="mt-0.5 size-4 text-success" aria-hidden /> : <ThumbsDown className="mt-0.5 size-4 text-destructive" aria-hidden />}
                <span>
                  <span className="font-medium">{a.decision === "approve" ? "Approved" : "Rejected"}</span> by {a.approver_id === reviewer.userId ? "you" : <PersonName id={a.approver_id} />} · {relativeTime(a.decided_at)}
                  {a.comment ? <span className="block text-muted-foreground">“{a.comment}”</span> : null}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[13px] text-muted-foreground">No decision yet.</p>
        )}
        {reviewer.isReviewer && canDecide ? (
          <div className="grid gap-3 rounded-md border p-3" data-testid="decision-form">
            {isSubmitter ? (
              <p role="note" className="text-[13px] text-muted-foreground">
                You submitted this validation, so a second person has to review it.
              </p>
            ) : null}
            <Field label="Comment" htmlFor="approval-comment" hint="Required to reject; recorded in the audit log." error={rejectNeedsComment ? "Say why you reject it." : null}>
              <Textarea id="approval-comment" maxLength={2000} value={comment} disabled={isSubmitter} onChange={(e) => setComment(e.target.value)} placeholder="Null tests are convincing; benchmarks beaten by 30%." />
            </Field>
            {decide.isError ? <ErrorNotice error={decide.error} title="The decision was not recorded" /> : null}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => send("approve")} disabled={isSubmitter || decide.isPending}>
                {decide.isPending && tried === "approve" ? <Spinner /> : <ThumbsUp />} Approve
              </Button>
              <Button variant="outline" className="text-destructive" onClick={() => send("reject")} disabled={isSubmitter || decide.isPending}>
                <ThumbsDown /> Reject
              </Button>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ExportPack({ validation }: { validation: ModelValidation }) {
  const { exportPack } = useValidationActions(validation.id);
  const [format, setFormat] = useState<"html" | "json">("html");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const pack = await exportPack.mutateAsync({ format, include_plots: true });
      const base = `evidence-pack-${validation.model_id}-v${validation.version}`;
      const link = pack.download as { url?: unknown; name?: unknown } | null | undefined;
      if (format === "html" && link && typeof link.url === "string") {
        // A static HTML file: saved, never rendered inside the app.
        const bytes = await fetchArtifactBytes(link as never);
        saveBytes(bytes, `${base}.html`);
      } else {
        saveBytes(new TextEncoder().encode(JSON.stringify(pack, null, 2)), `${base}.json`, "application/json");
      }
      toast.success("Evidence pack exported", pack.digest ? `digest ${pack.digest.slice(0, 19)}…` : undefined);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Format" htmlFor="pack-format">
          <NativeSelect id="pack-format" className="w-60" value={format} onChange={(e) => setFormat(e.target.value as "html" | "json")}>
            <option value="html">Readable page (HTML)</option>
            <option value="json">Data (JSON)</option>
          </NativeSelect>
        </Field>
        <Button variant="outline" onClick={() => void run()} disabled={busy}>
          {busy ? <Spinner /> : <Download />} Export evidence pack
        </Button>
      </div>
      {error ? <ErrorNotice error={error} title="The evidence pack was not exported" /> : null}
    </div>
  );
}

function NoteDialog({ title, description, label, required, action, onSubmit, onClose, pending, error }: { title: string; description: string; label: string; required: boolean; action: string; onSubmit: (note: string | null) => void; onClose: () => void; pending: boolean; error: unknown }) {
  const [note, setNote] = useState("");
  const [tried, setTried] = useState(false);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            setTried(true);
            if (required && !note.trim()) return;
            onSubmit(note.trim() || null);
          }}
        >
          <Field label={label} htmlFor="note-text" error={tried && required && !note.trim() ? "Required." : null}>
            <Textarea id="note-text" autoFocus maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          {error ? <ErrorNotice error={error} title={`${action} failed`} /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? <Spinner /> : null} {action}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ValidationView({ validationId }: { validationId: string }) {
  const ws = useWorkspace();
  const q = useValidation(validationId);
  const model = useModel(q.data?.model_id);
  const policies = usePolicies("all");
  const checks = useGovernanceChecks();
  const actions = useValidationActions(validationId);
  const [dialog, setDialog] = useState<"cancel" | "resubmit" | "deprecate" | null>(null);
  if (q.isPending) return <LoadingRows rows={6} />;
  if (q.isError || !q.data) return <ErrorNotice error={q.error} title="The validation could not be loaded" />;
  const v = q.data;
  const mayValidate = can(ws.grants, "validate", "model");
  const mayDeprecate = can(ws.grants, "update", "model");
  const results = v.results ?? [];
  const counts = { pass: 0, warn: 0, fail: 0, error: 0, skipped: 0 } as Record<string, number>;
  for (const r of results) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const mutation = dialog ? actions[dialog] : null;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5" data-testid="validation-view">
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            Validation of{" "}
            <Link to="/models/$modelId/versions/$version" params={{ modelId: v.model_id, version: String(v.version) }} className="hover:underline">
              {model.data?.name ?? "model"} v{v.version}
            </Link>
            <ValidationBadge status={v.status} />
          </span>
        }
        description={VALIDATION_META[v.status].plain}
        meta={
          <>
            <Mono>{v.model_id}</Mono>
            <span>·</span>
            <span>submitted {v.submitted_at ? relativeTime(v.submitted_at) : "—"}</span>
            {v.submitted_by ? <span>by {v.submitted_by === ws.user.id ? "you" : <PersonName id={v.submitted_by} />}</span> : null}
          </>
        }
        actions={
          <>
            {v.status === "checks_running" && mayValidate ? (
              <Button variant="outline" onClick={() => setDialog("cancel")}>
                <Ban /> Cancel checks
              </Button>
            ) : null}
            {(v.status === "checks_failed" || v.status === "rejected") && mayValidate ? (
              <Button variant="outline" onClick={() => setDialog("resubmit")}>
                <RotateCcw /> Resubmit
              </Button>
            ) : null}
            {v.status === "validated" && mayDeprecate ? (
              <Button variant="outline" onClick={() => setDialog("deprecate")}>
                <ShieldOff /> Deprecate
              </Button>
            ) : null}
          </>
        }
      />
      <Card>
        <CardHeader>
          <CardTitle>Status</CardTitle>
        </CardHeader>
        <CardContent>
          <StatusTimeline validation={v} />
        </CardContent>
      </Card>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <section className="grid content-start gap-3" aria-label="Check results">
          <h2 className="text-base font-semibold">Checks</h2>
          {v.status === "checks_running" ? (
            <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
              <Spinner /> The checks are running in the sandbox{v.workflow_run_id ? (
                <>
                  {" "}
                  (run{" "}
                  <Link to="/runs/$runId" params={{ runId: v.workflow_run_id }} className="text-primary hover:underline">
                    {shortId(v.workflow_run_id)}
                  </Link>
                  )
                </>
              ) : null}
              .
            </p>
          ) : null}
          {results.length ? (
            <>
              <p className="text-[13px]">
                {counts.pass ?? 0} passed · {counts.warn ?? 0} warned · {(counts.fail ?? 0) + (counts.error ?? 0)} failed · {counts.skipped ?? 0} did not apply
                {v.failing_checks?.length ? <span className="text-destructive"> · blocking: {v.failing_checks.join(", ")}</span> : null}
              </p>
              <CheckResultList results={results} checks={checks.map} />
            </>
          ) : v.status !== "checks_running" ? (
            <p className="text-[13px] text-muted-foreground">No results yet.</p>
          ) : null}
        </section>
        <aside className="grid content-start gap-5">
          <DecisionPanel validation={v} />
          <Card>
            <CardHeader>
              <CardTitle>Evidence pack</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3">
              <p className="text-[13px] text-muted-foreground">Everything an auditor needs in one file: the results, approvals, history, policies, lineage and a digest.</p>
              <ExportPack validation={v} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Held to</CardTitle>
            </CardHeader>
            <CardContent>
              <KeyValues
                items={[
                  [
                    "Policies",
                    <ul key="policies" className="grid gap-0.5">
                      {(v.policies ?? []).map((ref) => {
                        const [id = "", version = ""] = ref.split("@");
                        const p = policies.data?.items.find((x) => x.id === id);
                        return (
                          <li key={ref}>
                            {p ? (
                              <Link to="/governance/policies/$policyId" params={{ policyId: id }} search={p.scope === "tenant" ? { scope: "tenant" } : {}} className="text-primary hover:underline">
                                {p.name} v{version}
                              </Link>
                            ) : (
                              <Mono>{ref}</Mono>
                            )}
                          </li>
                        );
                      })}
                    </ul>,
                  ],
                  ["Checks", `${(v.required_checks ?? []).length} required`],
                  ["Updated", absoluteTime(v.updated_at)],
                ]}
              />
              <Button variant="link" className="mt-2 px-0" asChild>
                <Link to="/models/$modelId/versions/$version/validations" params={{ modelId: v.model_id, version: String(v.version) }}>
                  <FileCheck2 /> All validations of this version
                </Link>
              </Button>
            </CardContent>
          </Card>
        </aside>
      </div>
      {dialog && mutation ? (
        <NoteDialog
          title={dialog === "cancel" ? "Cancel the running checks?" : dialog === "resubmit" ? "Run the checks again?" : "Deprecate this validated version?"}
          description={
            dialog === "cancel"
              ? "The governance run stops and the validation goes back to draft."
              : dialog === "resubmit"
                ? "A new round starts under the policies in force now; the earlier results are kept in the history."
                : "It stays in the audit trail but should no longer be used; promotions that require validation will refuse it."
          }
          label={dialog === "deprecate" ? "Why should it no longer be used?" : "Note (optional)"}
          required={dialog === "deprecate"}
          action={dialog === "cancel" ? "Cancel checks" : dialog === "resubmit" ? "Resubmit" : "Deprecate"}
          pending={mutation.isPending}
          error={mutation.error}
          onClose={() => setDialog(null)}
          onSubmit={(note) => {
            const done = () => setDialog(null);
            if (dialog === "deprecate") actions.deprecate.mutate(note ?? "", { onSuccess: done });
            else actions[dialog].mutate(note, { onSuccess: done });
          }}
        />
      ) : null}
    </div>
  );
}
