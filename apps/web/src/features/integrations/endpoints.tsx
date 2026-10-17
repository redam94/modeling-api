/**
 * Endpoints and aliases: the list, the promotion dialog with the scorecard gate (and the
 * audited admin override), rollback and delete. An endpoint is a stable alias
 * (`endpoint://project/alias`) for one model version, so integrations never change when a
 * better model is promoted.
 */
import { Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, ArrowUpCircle, CheckCircle2, Plug, RotateCcw, ShieldAlert, Trash2, XCircle } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { ApiError, errorMessage } from "@/api/client";
import { useDeleteEndpoint, useEndpoints, usePromoteEndpoint, useRollbackEndpoint } from "@/api/integrations";
import { useModels, useModelVersion, useModelVersions } from "@/api/queries";
import type { Endpoint, EndpointPromote, EndpointRevision, EvaluationPolicy, GateOutcome, PromotionGateDecision, Scorecard } from "@/api/types";
import { EmptyState, ErrorNotice, Mono, QueryView, Spinner } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Badge, Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { absoluteTime, relativeTime } from "@/lib/format";
import { GATE_META, GATE_REASON_LABEL, gateFromProblem, previewGate } from "@/lib/gate";
import { NotValidatedNotice } from "@/features/governance/model-validations";
import { ALIAS_RE } from "@/lib/integrations";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

// ----------------------------------------------------------------------- gate widgets

export function GateBadge({ outcome, overridden = false }: { outcome: GateOutcome; overridden?: boolean }) {
  const meta = GATE_META[outcome];
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge variant={meta.variant} title={meta.description}>
        {meta.label}
      </Badge>
      {overridden ? (
        <Badge variant="outline" title="Promoted by an audited admin override">
          <ShieldAlert /> overridden
        </Badge>
      ) : null}
    </span>
  );
}

const REASON_ICON: Record<GateOutcome, React.ReactNode> = {
  allowed: <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />,
  needs_override: <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />,
  blocked: <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" />,
};

/** The reasons of a gate decision, in plain words with the check that raised each. */
export function GateReasons({ decision, empty = "Every check passes." }: { decision: PromotionGateDecision; empty?: string }) {
  const reasons = decision.reasons ?? [];
  if (!reasons.length) return <p className="text-[13px] text-muted-foreground">{empty}</p>;
  return (
    <ul className="grid gap-2" aria-label="Gate reasons">
      {reasons.map((r, i) => (
        <li key={`${r.code}-${r.check ?? ""}-${i}`} className="flex items-start gap-2 text-[13px]">
          {REASON_ICON[r.outcome]}
          <span className="min-w-0">
            <span className="font-medium">{GATE_REASON_LABEL[r.code]}</span>
            {r.check ? <Mono className="ml-2 text-muted-foreground">{r.check}</Mono> : null}
            <span className="block text-muted-foreground">{r.message_plain}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

const CARD_ICON = {
  pass: <CheckCircle2 className="size-4 text-success" />,
  warn: <AlertTriangle className="size-4 text-warning" />,
  fail: <XCircle className="size-4 text-destructive" />,
} as const;

/** A compact, plain-language scorecard (the Guided report has the full view). */
export function ScorecardSummary({ scorecard }: { scorecard: Scorecard | null | undefined }) {
  if (!scorecard) return <p className="text-[13px] text-muted-foreground">This version has no sanity scorecard.</p>;
  return (
    <ul className="grid gap-1.5" aria-label="Scorecard">
      {(scorecard.items ?? []).map((item) => (
        <li key={item.check ?? item.code} className="grid grid-cols-[1.25rem_1fr] gap-2 text-[13px]">
          <span className="pt-0.5">{CARD_ICON[item.status]}</span>
          <span className="min-w-0">
            <span className="font-medium">{item.title}</span>
            <span className="block text-muted-foreground">{item.message_plain}</span>
            {item.status !== "pass" && item.suggested_action ? <span className="block text-xs">{item.suggested_action}</span> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ----------------------------------------------------------------------- list

export function EndpointsSection() {
  const ws = useWorkspace();
  const endpoints = useEndpoints();
  const navigate = useNavigate();
  const [promoting, setPromoting] = useState(false);
  const canPromote = can(ws.grants, "promote", "endpoint");
  const columns = useMemo<ColumnDef<Endpoint, unknown>[]>(
    () => [
      {
        accessorKey: "alias",
        header: "Endpoint",
        cell: ({ row }) => (
          <div className="grid min-w-0">
            <Link to="/integrations/endpoints/$alias" params={{ alias: row.original.alias }} className="font-medium text-primary hover:underline">
              {row.original.alias}
            </Link>
            <Mono className="truncate text-muted-foreground">{row.original.ref}</Mono>
          </div>
        ),
      },
      { accessorKey: "model_ref", header: "Serves", cell: ({ row }) => <Mono>{row.original.model_ref}</Mono> },
      { accessorKey: "revision", header: "Revision", cell: ({ row }) => <span className="tabular">r{row.original.revision}</span> },
      {
        id: "gate",
        header: "Gate",
        accessorFn: (e) => e.gate.outcome,
        cell: ({ row }) => <GateBadge outcome={row.original.gate.outcome} overridden={Boolean(row.original.overridden)} />,
      },
      {
        accessorKey: "promoted_at",
        header: "Promoted",
        sortingFn: "datetime",
        cell: ({ row }) => <span title={absoluteTime(row.original.promoted_at)}>{relativeTime(row.original.promoted_at)}</span>,
      },
    ],
    [],
  );
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="max-w-[68ch] text-muted-foreground">
          An endpoint is a stable name for one model version. Spreadsheets, dashboards and notebooks call the name, so promoting a better version - or rolling back - never breaks them. Promotion is gated on the version&apos;s sanity scorecard.
        </p>
        <Button onClick={() => setPromoting(true)} disabled={!canPromote} title={canPromote ? undefined : "Project admins promote endpoints"}>
          <ArrowUpCircle /> Promote a model
        </Button>
      </div>
      <QueryView query={endpoints}>
        {(page) =>
          page.items.length ? (
            <DataTable
              label="Endpoints"
              data={page.items}
              columns={columns}
              getRowId={(e) => e.id}
              onRowClick={(e) => void navigate({ to: "/integrations/endpoints/$alias", params: { alias: e.alias } })}
              filterPlaceholder="Filter endpoints"
            />
          ) : (
            <EmptyState
              icon={<Plug />}
              title="No endpoints yet"
              action={
                canPromote ? (
                  <Button onClick={() => setPromoting(true)}>
                    <ArrowUpCircle /> Promote your first model
                  </Button>
                ) : null
              }
            >
              Promote a fitted model version to an endpoint to score it from a spreadsheet, a BI tool, a notebook or your own service.
            </EmptyState>
          )
        }
      </QueryView>
      {promoting ? (
        <PromoteDialog
          onClose={() => setPromoting(false)}
          onPromoted={(ep) => {
            setPromoting(false);
            void navigate({ to: "/integrations/endpoints/$alias", params: { alias: ep.alias } });
          }}
        />
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------------------- promote

const FIT_MODES: { value: EvaluationPolicy["fit_mode"] & string; label: string }[] = [
  { value: "evaluate_then_refit", label: "Evaluated, then refitted on all data (required for production)" },
  { value: "train_only", label: "Also accept exploration fits (train only)" },
];

const SCHEMA_CHANGES: { value: NonNullable<EvaluationPolicy["schema_change"]>; label: string }[] = [
  { value: "block", label: "Block versions that expect different input" },
  { value: "warn", label: "Allow them with an admin override" },
  { value: "allow", label: "Allow them (callers must follow)" },
];

/**
 * Promote a model version to an endpoint (new or existing). The gate is previewed from the
 * version's scorecard; the server's decision comes back as `409 OVERRIDE_REQUIRED` (a signed-in
 * project admin may override with a reason) or `PROMOTION_BLOCKED` (no override).
 */
export function PromoteDialog({ endpoint, onClose, onPromoted }: { endpoint?: Endpoint; onClose: () => void; onPromoted: (ep: Endpoint) => void }) {
  const ws = useWorkspace();
  const canOverride = can(ws.grants, "manage", "endpoint");
  const promote = usePromoteEndpoint();
  const models = useModels();
  const [alias, setAlias] = useState(endpoint?.alias ?? "");
  const [modelId, setModelId] = useState<string>(endpoint?.model_id ?? "");
  const firstModel = models.data?.items[0]?.id ?? "";
  const effectiveModel = modelId || firstModel;
  const versions = useModelVersions(effectiveModel || undefined);
  const [version, setVersion] = useState<number | null>(null);
  const latest = versions.data?.items.reduce((m, v) => Math.max(m, v.version), 0) || undefined;
  const effectiveVersion = version ?? latest;
  const mv = useModelVersion(effectiveModel || undefined, effectiveVersion);
  const [description, setDescription] = useState(endpoint?.description ?? "");
  const [fitMode, setFitMode] = useState<string>(endpoint?.evaluation_policy?.fit_mode ?? "evaluate_then_refit");
  const [schemaChange, setSchemaChange] = useState<NonNullable<EvaluationPolicy["schema_change"]>>(endpoint?.evaluation_policy?.schema_change ?? "block");
  const [decision, setDecision] = useState<PromotionGateDecision | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [stale, setStale] = useState(false);

  const aliasError = !endpoint && !ALIAS_RE.test(alias) ? "Lowercase letters, digits, - and _ (up to 128), starting and ending with a letter or digit." : null;
  const reasonError = decision?.outcome === "needs_override" && !overrideReason.trim() ? "Say why this version should serve despite the gate; the reason is audited." : null;
  const preview = mv.data ? previewGate(mv.data) : null;
  const policyChanged =
    !endpoint || fitMode !== (endpoint.evaluation_policy?.fit_mode ?? "evaluate_then_refit") || schemaChange !== (endpoint.evaluation_policy?.schema_change ?? "block");

  const send = (override: boolean) => {
    if (!mv.data) return;
    const body: EndpointPromote = {
      model_version_id: mv.data.id,
      expected_revision: endpoint?.revision ?? 0,
      ...(description.trim() && description !== (endpoint?.description ?? "") ? { description: description.trim() } : {}),
      ...(policyChanged ? { evaluation_policy: { fit_mode: fitMode as EvaluationPolicy["fit_mode"], schema_change: schemaChange } } : {}),
      ...(override ? { override: true, reason: overrideReason.trim() } : {}),
    };
    promote.mutate(
      { alias: endpoint?.alias ?? alias, body },
      {
        onSuccess: (ep) => {
          toast.success(override ? "Promoted by override" : "Promoted", `${ep.alias} now serves ${ep.model_ref} (r${ep.revision})`);
          onPromoted(ep);
        },
        onError: (e) => {
          const gate = e instanceof ApiError ? gateFromProblem(e.problem) : null;
          if (gate) setDecision(gate);
          if (e instanceof ApiError && e.code === "CONFLICT") setStale(true);
        },
      },
    );
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (aliasError) return;
    if (decision?.outcome === "needs_override") {
      if (reasonError || !canOverride) return;
      send(true);
      return;
    }
    send(false);
  };

  const pickVersion = (next: number | null) => {
    setVersion(next);
    setDecision(null);
    promote.reset();
  };
  const refused = promote.error instanceof ApiError && gateFromProblem(promote.error.problem);
  const otherError = promote.isError && !refused;

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>{endpoint ? `Promote a version to ${endpoint.alias}` : "Promote a model to an endpoint"}</DialogTitle>
        <DialogDescription>
          {endpoint
            ? `Callers of ${endpoint.ref} get the new version from their next call. It serves r${endpoint.revision} now.`
            : "Pick a fitted model version and name the endpoint integrations will call."}
        </DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          {!endpoint ? (
            <Field label="Endpoint name" htmlFor="ep-alias" error={submitted ? aliasError : null} hint="Integrations call endpoint://<project>/<name>; it cannot be renamed later.">
              <Input id="ep-alias" className="font-mono" autoFocus placeholder="geo-mmm-prod" value={alias} onChange={(e) => setAlias(e.target.value.toLowerCase())} />
            </Field>
          ) : null}
          <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <Field label="Model" htmlFor="ep-model">
              <NativeSelect
                id="ep-model"
                value={effectiveModel}
                onChange={(e) => {
                  setModelId(e.target.value);
                  pickVersion(null);
                }}
                disabled={!models.data?.items.length}
              >
                {models.data?.items.length ? null : <option value="">No models yet</option>}
                {models.data?.items.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Version" htmlFor="ep-version">
              <NativeSelect id="ep-version" value={effectiveVersion ?? ""} onChange={(e) => pickVersion(Number(e.target.value))} disabled={!versions.data?.items.length}>
                {versions.data?.items.map((v) => (
                  <option key={v.id} value={v.version}>
                    v{v.version}
                    {v.status === "partial" ? " (partial)" : ""}
                    {v.scorecard ? ` · ${v.scorecard.overall}` : ""}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>

          <section aria-label="Promotion gate" className="grid gap-3 rounded-lg border p-4">
            {mv.isPending && effectiveVersion ? (
              <span className="flex items-center gap-2 text-muted-foreground">
                <Spinner /> Loading the version…
              </span>
            ) : mv.data && preview ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-medium">
                    <Mono>{mv.data.ref}</Mono>
                  </p>
                  <span className="flex items-center gap-2 text-xs text-muted-foreground">
                    {decision ? "The gate says" : "Expected gate"} <GateBadge outcome={(decision ?? preview).outcome} />
                  </span>
                </div>
                <ScorecardSummary scorecard={mv.data.scorecard} />
                {decision?.reasons?.some((r) => r.code === "NOT_VALIDATED") ? (
                  <NotValidatedNotice modelId={mv.data.model_id} version={mv.data.version} />
                ) : decision ? (
                  <div className={cn("grid gap-2 rounded-md border p-3", decision.outcome === "blocked" ? "border-destructive/35 bg-destructive/6" : "border-warning/40 bg-warning/8")} role="alert">
                    <p className="font-medium">{decision.outcome === "blocked" ? "This version cannot be promoted" : "The gate needs an admin override"}</p>
                    <GateReasons decision={decision} />
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">The server also checks that the version was refitted on all data and that its input contract still matches what callers send.</p>
                )}
              </>
            ) : (
              <p className="text-muted-foreground">Fit a model first: endpoints serve model versions.</p>
            )}
          </section>

          {decision?.outcome === "needs_override" ? (
            canOverride ? (
              <Field label="Override reason" htmlFor="ep-reason" error={submitted ? reasonError : null} hint="Recorded in the audit log with your name (endpoint.promote_override). API tokens can never override.">
                <Textarea id="ep-reason" maxLength={1000} value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} placeholder="Holdout error accepted for the Q4 plan; refit after the holidays." />
              </Field>
            ) : (
              <p className="text-[13px] text-muted-foreground">Only a project admin signed in to the app can override the gate. Ask one to promote this version, or improve the model.</p>
            )
          ) : null}

          <details className="rounded-md border p-3 text-[13px]">
            <summary className="cursor-pointer font-medium">Description and evaluation policy</summary>
            <div className="mt-3 grid gap-4">
              <Field label="Description (optional)" htmlFor="ep-desc">
                <Input id="ep-desc" maxLength={2000} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Weekly budget planning for the EU markets" />
              </Field>
              <div className="grid gap-4">
                <Field label="Accepted fits" htmlFor="ep-fit">
                  <NativeSelect id="ep-fit" value={fitMode} onChange={(e) => setFitMode(e.target.value)}>
                    {FIT_MODES.map((f) => (
                      <option key={f.value} value={f.value}>
                        {f.label}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                <Field label="Input contract changes" htmlFor="ep-schema">
                  <NativeSelect id="ep-schema" value={schemaChange} onChange={(e) => setSchemaChange(e.target.value as typeof schemaChange)}>
                    {SCHEMA_CHANGES.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
              </div>
            </div>
          </details>

          {stale ? (
            <p role="alert" className="text-[13px] text-warning">
              Someone else promoted this endpoint meanwhile. Close the dialog to see its current version, then try again.
            </p>
          ) : otherError ? (
            <ErrorNotice error={promote.error} title="The version was not promoted" />
          ) : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            {decision?.outcome === "needs_override" ? (
              <Button type="submit" variant="destructive" disabled={promote.isPending || !canOverride}>
                <ShieldAlert /> Override and promote
              </Button>
            ) : (
              <Button type="submit" disabled={promote.isPending || !mv.data || decision?.outcome === "blocked" || stale}>
                {promote.isPending ? <Spinner /> : <ArrowUpCircle />} Promote
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ----------------------------------------------------------------------- rollback & delete

export function RollbackDialog({ endpoint, target, onClose }: { endpoint: Endpoint; target?: EndpointRevision; onClose: () => void }) {
  const rollback = useRollbackEndpoint(endpoint.alias);
  const [reason, setReason] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const error = !reason.trim() ? "Say why; the rollback is audited with its reason." : null;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Roll {endpoint.alias} back?</DialogTitle>
        <DialogDescription>
          {target ? (
            <>
              It will serve <Mono>{target.model_ref}</Mono> again (from r{target.revision}).
            </>
          ) : (
            <>It will serve the version of the revision before r{endpoint.revision} again.</>
          )}{" "}
          The gate is not re-run: that version was promoted before.
        </DialogDescription>
        <form
          className="grid gap-4"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            setSubmitted(true);
            if (error) return;
            rollback.mutate(
              { reason: reason.trim(), ...(target ? { to_revision: target.revision } : {}) },
              {
                onSuccess: (ep) => {
                  toast.success("Rolled back", `${ep.alias} serves ${ep.model_ref} (r${ep.revision})`);
                  onClose();
                },
              },
            );
          }}
        >
          <Field label="Reason" htmlFor="rb-reason" error={submitted ? error : null}>
            <Textarea id="rb-reason" autoFocus maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="v3 over-forecasts the holiday weeks" />
          </Field>
          {rollback.isError ? <ErrorNotice error={rollback.error} title="The endpoint was not rolled back" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={rollback.isPending}>
              <RotateCcw /> Roll back
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function DeleteEndpointDialog({ endpoint, onClose, onDeleted }: { endpoint: Endpoint; onClose: () => void; onDeleted: () => void }) {
  const del = useDeleteEndpoint();
  const [typed, setTyped] = useState("");
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Delete {endpoint.alias}?</DialogTitle>
        <DialogDescription>
          Every integration calling <Mono>{endpoint.ref}</Mono> gets 404 Not found from its next call. The model version itself is kept.
        </DialogDescription>
        <Field label={`Type ${endpoint.alias} to confirm`} htmlFor="ep-del">
          <Input id="ep-del" className="font-mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
        </Field>
        {del.isError ? <ErrorNotice error={del.error} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            disabled={typed !== endpoint.alias || del.isPending}
            onClick={() =>
              del.mutate(endpoint.alias, {
                onSuccess: () => {
                  toast.success("Endpoint deleted", endpoint.alias);
                  onDeleted();
                },
                onError: (e) => toast.error("Delete failed", errorMessage(e)),
              })
            }
          >
            <Trash2 /> Delete endpoint
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
