/**
 * Operator console additions for the trust lifecycle: the sign-up review queue (sign-ups the
 * Sybil heuristics flagged), and per tenant its identity verification (which gates uploaded
 * code) and the status of its deletion, with a retry for a failed purge.
 */
import { CheckCircle2, RefreshCw, ShieldCheck, UserCheck, XCircle } from "lucide-react";
import { useState, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { useAdminSignups, usePutTenantVerification, useRetryTenantDeletion, useReviewSignup, useTenantDeletion, useTenantVerification } from "@/api/trust";
import type { SignupReview, SignupReviewState, Tenant, VerificationMethod } from "@/api/types";
import { EmptyState, ErrorNotice, KeyValues, Mono, QueryView, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Badge, Field, NativeSelect, Separator, TBody, TD, TH, THead, TR, Table, Textarea } from "@/components/ui/primitives";
import { absoluteTime, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { DeletionProgress } from "./deletion-progress";

const REVIEW_FILTERS: { value: SignupReviewState | ""; label: string }[] = [
  { value: "flagged", label: "Waiting for review" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
  { value: "", label: "All sign-ups" },
];

const REVIEW_VARIANT: Record<SignupReviewState, "outline" | "warning" | "success" | "danger"> = { none: "outline", flagged: "warning", approved: "success", rejected: "danger" };

function ReviewDialog({ signup, decision, onClose }: { signup: SignupReview; decision: "approve" | "reject"; onClose: () => void }) {
  const review = useReviewSignup();
  const [reason, setReason] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const error = reason.trim() ? null : "Type the reason. The audit log records it with the decision.";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (error) return;
    review.mutate(
      { signupId: signup.id, body: { decision, reason: reason.trim() } },
      {
        onSuccess: () => {
          toast.success(decision === "approve" ? "Sign-up approved" : "Sign-up rejected", signup.email);
          onClose();
        },
      },
    );
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>
          {decision === "approve" ? "Approve" : "Reject"} {signup.email}?
        </DialogTitle>
        <DialogDescription>
          {decision === "approve"
            ? "The app removes the flag. The workspace continues to operate."
            : signup.tenant_id
              ? `The app suspends the workspace ${signup.tenant_id} that this sign-up created. The workspace becomes read-only.`
              : "The sign-up has no workspace yet. Its link will not work after this."}
        </DialogDescription>
        <form className="grid gap-4" onSubmit={submit} noValidate>
          <Field label="Reason" htmlFor="review-reason" error={submitted ? error : null}>
            <Textarea id="review-reason" autoFocus maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          {review.isError ? <ErrorNotice error={review.error} /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant={decision === "approve" ? "default" : "destructive"} disabled={review.isPending}>
              {decision === "approve" ? <CheckCircle2 /> : <XCircle />} {decision === "approve" ? "Approve" : "Reject"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Admin console → Sign-ups: the Sybil review queue. */
export function SignupsTab() {
  const [review, setReview] = useState<SignupReviewState | "">("flagged");
  const signups = useAdminSignups(review || null);
  const [deciding, setDeciding] = useState<{ s: SignupReview; d: "approve" | "reject" } | null>(null);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <NativeSelect aria-label="Filter sign-ups" className="h-8 w-48" value={review} onChange={(e) => setReview(e.target.value as SignupReviewState | "")}>
          {REVIEW_FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </NativeSelect>
        <p className="text-[13px] text-muted-foreground">A flag never stops a sign-up. It puts the sign-up in this queue. Network addresses and browsers show only as keyed digests.</p>
      </div>
      <QueryView query={signups}>
        {(page) =>
          page.items.length ? (
            <Table aria-label="Sign-ups">
              <THead>
                <TR>
                  <TH>Sign-up</TH>
                  <TH>Why it was flagged</TH>
                  <TH>State</TH>
                  <TH>Review</TH>
                  <TH>
                    <span className="sr-only">Actions</span>
                  </TH>
                </TR>
              </THead>
              <TBody>
                {page.items.map((s) => (
                  <TR key={s.id}>
                    <TD>
                      <span className="font-medium">{s.name}</span>
                      <span className="block font-mono text-[12px]">{s.email}</span>
                      <span className="block text-xs text-muted-foreground">
                        {s.organisation ? `${s.organisation} · ` : ""}
                        {relativeTime(s.created_at)}
                        {s.tenant_id ? (
                          <>
                            {" "}
                            · workspace <Mono>{s.tenant_id}</Mono>
                          </>
                        ) : null}
                      </span>
                    </TD>
                    <TD className="max-w-80">
                      {s.flags?.length ? (
                        <ul className="grid gap-1">
                          {(s.flags ?? []).map((f, i) => (
                            <li key={f} className="text-[13px]">
                              <Badge variant="warning" className="font-mono text-[11px]">
                                {f}
                              </Badge>{" "}
                              <span className="text-muted-foreground">{s.reasons_plain?.[i] ?? ""}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <span className="text-muted-foreground">Not flagged</span>
                      )}
                      {s.ip_hash || s.device_hash ? (
                        <span className="mt-1 block text-[11px] text-muted-foreground">
                          {s.ip_hash ? <>address {s.ip_hash} </> : null}
                          {s.device_hash ? <>browser {s.device_hash}</> : null}
                        </span>
                      ) : null}
                    </TD>
                    <TD>
                      <Badge variant={s.state === "verified" ? "success" : "outline"}>{s.state}</Badge>
                    </TD>
                    <TD>
                      <Badge variant={REVIEW_VARIANT[s.review ?? "none"]}>{s.review ?? "none"}</Badge>
                      {s.review_reason ? <span className="block max-w-56 text-xs text-muted-foreground">{s.review_reason}</span> : null}
                    </TD>
                    <TD className="text-right whitespace-nowrap">
                      {s.review === "flagged" ? (
                        <>
                          <Button size="sm" variant="ghost" onClick={() => setDeciding({ s, d: "approve" })} aria-label={`Approve ${s.email}`}>
                            <CheckCircle2 /> Approve
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => setDeciding({ s, d: "reject" })} aria-label={`Reject ${s.email}`}>
                            <XCircle /> Reject
                          </Button>
                        </>
                      ) : null}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : (
            <EmptyState icon={<UserCheck />} title={review === "flagged" ? "No sign-ups wait for review" : "No sign-ups agree with the filter"}>
              Sign-ups that trigger a Sybil heuristic wait here for a decision. The heuristics are: same address, same browser, repeated e-mail and a burst from one domain.
            </EmptyState>
          )
        }
      </QueryView>
      {deciding ? <ReviewDialog signup={deciding.s} decision={deciding.d} onClose={() => setDeciding(null)} /> : null}
    </div>
  );
}

const METHODS: { value: VerificationMethod; label: string }[] = [
  { value: "document", label: "Identity document" },
  { value: "card", label: "Payment card" },
  { value: "manual", label: "Manual check (contract, call)" },
  { value: "email", label: "E-mail only" },
];

function VerificationSection({ tenant }: { tenant: Tenant }) {
  const status = useTenantVerification(tenant.id);
  const put = usePutTenantVerification(tenant.id);
  const [method, setMethod] = useState<VerificationMethod>("document");
  const [reason, setReason] = useState("");
  return (
    <section aria-label="Identity verification" className="grid gap-3">
      <h3 className="flex items-center gap-2 font-semibold">
        <ShieldCheck className="size-4" /> Identity verification
      </h3>
      <QueryView query={status}>
        {(v) => (
          <>
            <KeyValues
              items={[
                ["E-mail", v.email_verified ? "verified" : "not verified"],
                ["Identity", v.identity_verified ? `verified (${v.method ?? "manual"}) ${v.verified_at ? relativeTime(v.verified_at) : ""}` : "not verified"],
                ["Uploaded code", v.uploaded_code_allowed ? <Badge key="u" variant="success">allowed</Badge> : <Badge key="u" variant="outline">not allowed</Badge>],
              ]}
            />
            <p className="text-[13px] text-muted-foreground">{v.reason_plain}</p>
            <form
              className="grid gap-3 rounded-lg border p-3"
              onSubmit={(e) => {
                e.preventDefault();
                if (!reason.trim()) return;
                put.mutate(
                  { identity_verified: !v.identity_verified, method: v.identity_verified ? (v.method ?? "manual") : method, reason: reason.trim() },
                  {
                    onSuccess: () => {
                      toast.success(v.identity_verified ? "Verification withdrawn" : "Identity marked verified", tenant.name);
                      setReason("");
                    },
                    onError: (err) => toast.error("The change did not save", errorMessage(err)),
                  },
                );
              }}
            >
              {!v.identity_verified ? (
                <Field label="How did you do a check of the identity?" htmlFor="ver-method">
                  <NativeSelect id="ver-method" value={method} onChange={(e) => setMethod(e.target.value as VerificationMethod)}>
                    {METHODS.map((m) => (
                      <option key={m.value} value={m.value}>
                        {m.label}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
              ) : null}
              <Field label="Reason (audited)" htmlFor="ver-reason">
                <Textarea id="ver-reason" className="min-h-14" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={v.identity_verified ? "Document expired" : "Passport checked by the trust team, ticket T-1234"} />
              </Field>
              <Button type="submit" size="sm" variant={v.identity_verified ? "outline" : "default"} className="w-fit" disabled={!reason.trim() || put.isPending}>
                {put.isPending ? <Spinner /> : <ShieldCheck />} {v.identity_verified ? "Withdraw verification" : "Mark identity verified"}
              </Button>
              {tenant.trust_tier === "public" && !v.identity_verified ? (
                <p className="text-xs text-muted-foreground">Public tenants can use only built-in models until cut line B, also after verification. To let a tenant upload code, move it to the verified tier.</p>
              ) : null}
            </form>
          </>
        )}
      </QueryView>
    </section>
  );
}

function DeletionSection({ tenant }: { tenant: Tenant }) {
  const deletion = useTenantDeletion(tenant.id);
  const retry = useRetryTenantDeletion(tenant.id);
  return (
    <section aria-label="Deletion" className="grid gap-3">
      <h3 className="font-semibold">Deletion</h3>
      <QueryView query={deletion}>
        {(d) =>
          d ? (
            <>
              <DeletionProgress deletion={d} />
              <p className="text-xs text-muted-foreground">
                <Mono>{d.id}</Mono> · requested {absoluteTime(d.requested_at)}
                {d.requested_by ? (
                  <>
                    {" "}
                    by <Mono>{d.requested_by}</Mono>
                  </>
                ) : null}
              </p>
              {d.state === "failed" ? (
                <Button
                  size="sm"
                  className="w-fit"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate(undefined, { onSuccess: () => toast.success("Purge restarted", tenant.name), onError: (e) => toast.error("The purge did not start again", errorMessage(e)) })}
                >
                  {retry.isPending ? <Spinner /> : <RefreshCw />} Retry the purge
                </Button>
              ) : null}
            </>
          ) : (
            <p className="text-[13px] text-muted-foreground">The tenant did not ask for deletion.</p>
          )
        }
      </QueryView>
    </section>
  );
}

/** Admin console → Tenants → Trust: verification and deletion of one tenant. */
export function TenantTrustDialog({ tenant, onClose }: { tenant: Tenant; onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>Trust: {tenant.name}</DialogTitle>
        <DialogDescription>
          <Mono>{tenant.id}</Mono> · {tenant.trust_tier} tier · {tenant.status ?? "active"}
        </DialogDescription>
        <VerificationSection tenant={tenant} />
        <Separator />
        <DeletionSection tenant={tenant} />
      </DialogContent>
    </Dialog>
  );
}
