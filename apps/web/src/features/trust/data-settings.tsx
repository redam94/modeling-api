/**
 * Settings → Data (organisation admins): a GDPR export of everything the organisation stored,
 * and deleting the organisation - scheduled with a grace period in which it is suspended and
 * the deletion can be cancelled, then purged store by store.
 */
import { AlertTriangle, Archive, Download, Trash2, Undo2 } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { errorMessage } from "@/api/client";
import { DELETION_GRACE_DAYS, useCancelTenantDeletion, useCreateTenantExport, useRequestTenantDeletion, useTenantDeletion, useTenantExport } from "@/api/trust";
import type { GdprExport, GdprExportRequest, Tenant } from "@/api/types";
import { ErrorNotice, Mono, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, Textarea } from "@/components/ui/primitives";
import { absoluteTime, formatBytes, relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { DeletionProgress } from "./deletion-progress";

type Part = NonNullable<GdprExportRequest["include"]>[number];

const PARTS: { id: Part; label: string; description: string }[] = [
  { id: "metadata", label: "Workspace records", description: "Projects, members, specs, runs, endpoints, webhooks (credentials redacted)." },
  { id: "datasets", label: "Datasets", description: "Every uploaded file and ingested version." },
  { id: "models", label: "Models", description: "Bundle manifests of every model version." },
  { id: "audit", label: "Audit log", description: "Who did what, and when." },
  { id: "usage", label: "Usage and cost", description: "The cost ledger of every run." },
];

const EXPORT_VARIANT: Record<GdprExport["state"], "info" | "success" | "outline" | "danger"> = { queued: "info", running: "info", ready: "success", expired: "outline", failed: "danger" };

const lastExportKey = (tenant: string) => `mapi.export.${tenant}`;

function readLastExport(tenant: string): string | null {
  try {
    const v = localStorage.getItem(lastExportKey(tenant));
    return v && /^exp_[0-9a-z]{26}$/.test(v) ? v : null;
  } catch {
    return null;
  }
}

/** A signed link we are willing to hand to the browser: http(s) only. */
export function safeDownloadUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}

export function ExportCard({ tenant }: { tenant: Tenant }) {
  const [parts, setParts] = useState<Part[]>(PARTS.map((p) => p.id));
  const [exportId, setExportId] = useState<string | null>(() => readLastExport(tenant.id));
  const create = useCreateTenantExport(tenant.id);
  const current = useTenantExport(tenant.id, exportId);
  useEffect(() => {
    try {
      if (exportId) localStorage.setItem(lastExportKey(tenant.id), exportId);
    } catch {
      // the last export is only a convenience
    }
  }, [exportId, tenant.id]);
  const exp = current.data;
  const href = exp?.state === "ready" ? safeDownloadUrl(exp.download_url) : null;
  const working = exp?.state === "queued" || exp?.state === "running";
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Export your data</CardTitle>
          <CardDescription>One archive of everything {tenant.name} stored (GDPR access). It works while the organisation is suspended, too.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <fieldset className="grid gap-2 sm:grid-cols-2">
          <legend className="mb-1 text-[13px] font-medium">Include</legend>
          {PARTS.map((p) => (
            <label key={p.id} htmlFor={`exp-${p.id}`} className="flex cursor-pointer items-start gap-3 rounded-lg border p-3">
              <Checkbox id={`exp-${p.id}`} className="mt-0.5" checked={parts.includes(p.id)} onCheckedChange={(v) => setParts(v === true ? [...parts, p.id] : parts.filter((x) => x !== p.id))} />
              <span className="grid gap-0.5">
                <span className="text-[13px] font-medium">{p.label}</span>
                <span className="text-xs text-muted-foreground">{p.description}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            disabled={!parts.length || create.isPending || working}
            onClick={() =>
              create.mutate(
                { include: parts },
                {
                  onSuccess: (e) => {
                    setExportId(e.id);
                    toast.info("Export requested", "The archive is packed in the background; this page updates when it is ready.");
                  },
                },
              )
            }
          >
            {create.isPending ? <Spinner /> : <Archive />} Request export
          </Button>
          {!parts.length ? <span className="text-xs text-destructive">Pick at least one part.</span> : null}
        </div>
        {create.isError ? <ErrorNotice error={create.error} title="The export was not requested" /> : null}
        {exp ? (
          <div className="grid gap-2 rounded-lg border p-3 text-[13px]" aria-live="polite" data-testid="export-status">
            <p className="flex flex-wrap items-center gap-2">
              <Badge variant={EXPORT_VARIANT[exp.state]}>{exp.state}</Badge>
              <Mono className="text-muted-foreground">{exp.id}</Mono>
              <span className="text-muted-foreground">requested {relativeTime(exp.created_at)}</span>
            </p>
            {working ? (
              <p className="flex items-center gap-2 text-muted-foreground">
                <Spinner /> Packing {exp.include.join(", ")}…
              </p>
            ) : null}
            {href ? (
              <div className="flex flex-wrap items-center gap-3">
                <Button asChild size="sm">
                  <a href={href} rel="noopener noreferrer" referrerPolicy="no-referrer" download>
                    <Download /> Download archive{exp.size_bytes != null ? ` (${formatBytes(exp.size_bytes)})` : ""}
                  </a>
                </Button>
                <span className="text-xs text-muted-foreground">The link works until {absoluteTime(exp.expires_at)} (15 minutes); reload the page for a fresh one.</span>
              </div>
            ) : null}
            {exp.state === "expired" ? <p className="text-muted-foreground">The archive expired. Request a new export.</p> : null}
            {exp.state === "failed" ? <p className="text-destructive">The export failed. Request it again; operators were notified.</p> : null}
          </div>
        ) : current.isError ? (
          <ErrorNotice error={current.error} title="The last export could not be loaded" />
        ) : null}
      </CardContent>
    </Card>
  );
}

function DeleteTenantDialog({ tenant, onClose }: { tenant: Tenant; onClose: () => void }) {
  const request = useRequestTenantDeletion(tenant.id);
  const [typed, setTyped] = useState("");
  const [reason, setReason] = useState("");
  const [understood, setUnderstood] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (typed !== tenant.id || !understood) return;
    request.mutate(
      { confirm: typed, ...(reason.trim() ? { reason: reason.trim() } : {}) },
      {
        onSuccess: (d) => {
          toast.success("Deletion scheduled", `${tenant.name} is suspended; the purge starts ${absoluteTime(d.purge_after)}.`);
          onClose();
        },
      },
    );
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>Delete {tenant.name}?</DialogTitle>
        <DialogDescription>
          This deletes the whole organisation: every project, dataset, model, endpoint, webhook, token and member. Runs are cancelled and the organisation becomes read-only at once.
        </DialogDescription>
        <form className="grid gap-4" onSubmit={submit} noValidate>
          <ul className="grid list-disc gap-1 pl-5 text-[13px]">
            <li>You have {DELETION_GRACE_DAYS} days to change your mind: cancel here until the purge starts.</li>
            <li>Then every store is purged and cannot be recovered. Export your data first if you need it.</li>
            <li>The audit log (pseudonymised) and the cost ledger (amounts only) are kept for their legal retention.</li>
          </ul>
          <Field label={`Type ${tenant.id} to confirm`} htmlFor="del-confirm" hint="The organisation id, exactly.">
            <Input id="del-confirm" className="font-mono" autoComplete="off" spellCheck={false} value={typed} onChange={(e) => setTyped(e.target.value.trim())} />
          </Field>
          <Field label="Reason (optional)" htmlFor="del-reason">
            <Textarea id="del-reason" maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          <label className="flex items-start gap-2 text-[13px]">
            <Checkbox className="mt-0.5" checked={understood} onCheckedChange={(v) => setUnderstood(v === true)} />I understand that after {DELETION_GRACE_DAYS} days this cannot be undone.
          </label>
          {request.isError ? <ErrorNotice error={request.error} title="The deletion was not scheduled" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Keep the organisation
            </Button>
            <Button type="submit" variant="destructive" disabled={typed !== tenant.id || !understood || request.isPending}>
              <Trash2 /> Schedule deletion
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function DeletionCard({ tenant }: { tenant: Tenant }) {
  const deletion = useTenantDeletion(tenant.id);
  const cancel = useCancelTenantDeletion(tenant.id);
  const [asking, setAsking] = useState(false);
  const d = deletion.data;
  const active = d && (d.state === "scheduled" || d.state === "deleting" || d.state === "failed");
  return (
    <Card className="border-destructive/30">
      <CardHeader>
        <div>
          <CardTitle>Delete the organisation</CardTitle>
          <CardDescription>
            Removes {tenant.name} and everything in it after a {DELETION_GRACE_DAYS}-day grace period.
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        {deletion.isPending ? (
          <span className="flex items-center gap-2 text-muted-foreground">
            <Spinner /> Checking for a deletion…
          </span>
        ) : deletion.isError ? (
          <ErrorNotice error={deletion.error} />
        ) : (
          <>
            {d ? <DeletionProgress deletion={d} /> : null}
            {d?.state === "scheduled" ? (
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  disabled={cancel.isPending}
                  onClick={() =>
                    cancel.mutate(undefined, {
                      onSuccess: () => toast.success("Deletion cancelled", `${tenant.name} is active again.`),
                      onError: (e) => toast.error("Cancel failed", errorMessage(e)),
                    })
                  }
                >
                  {cancel.isPending ? <Spinner /> : <Undo2 />} Cancel the deletion
                </Button>
                <span className="text-xs text-muted-foreground">Possible until {absoluteTime(d.purge_after)}.</span>
              </div>
            ) : null}
            {d?.state === "failed" ? (
              <p className="flex items-start gap-2 text-[13px] text-destructive">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" /> The purge stopped on an error. It is retried automatically, and operators were notified.
              </p>
            ) : null}
            {!active ? (
              <Button variant="destructive" className="w-fit" onClick={() => setAsking(true)}>
                <Trash2 /> Delete {tenant.name}…
              </Button>
            ) : null}
          </>
        )}
      </CardContent>
      {asking ? <DeleteTenantDialog tenant={tenant} onClose={() => setAsking(false)} /> : null}
    </Card>
  );
}

/** Settings → Data, for organisation admins. */
export function DataSettings({ tenant }: { tenant: Tenant }) {
  return (
    <section id="data" aria-labelledby="data-heading" className="grid gap-5">
      <h2 id="data-heading" className="pt-2 text-lg font-semibold tracking-tight">
        Data
      </h2>
      <ExportCard tenant={tenant} />
      <DeletionCard tenant={tenant} />
    </section>
  );
}
