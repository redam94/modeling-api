/**
 * Validations of one model version: request a new one (held to every policy in force, plus
 * any the requester adds), and every earlier validation with its status. Also the notice the promotion
 * dialog shows when a policy requires a validated version (`NOT_VALIDATED`).
 */
import { Link, useNavigate } from "@tanstack/react-router";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

import { can } from "@/api/access";
import { useModel } from "@/api/queries";
import { EmptyState, ErrorNotice, Mono, PageHeader, QueryView, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Table, TBody, TD, TH, THead, TR, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { DatasetRefPicker } from "@/features/workflows/run-inputs";
import { relativeTime, shortId } from "@/lib/format";
import { toast } from "@/stores/toast";

import { useModelValidations, usePolicies, useRequestValidation } from "./api";
import { ValidationBadge } from "./badge";

function RequestForm({ modelId, version }: { modelId: string; version: number }) {
  const navigate = useNavigate();
  const policies = usePolicies("all");
  const request = useRequestValidation(modelId, version);
  const [note, setNote] = useState("");
  const [dataset, setDataset] = useState("");
  // Every policy in force always applies (the server never drops one); these are extra.
  const [extra, setExtra] = useState<string[]>([]);
  const items = policies.data?.items ?? [];
  const submit = (e: FormEvent) => {
    e.preventDefault();
    request.mutate(
      { note: note.trim() || null, policy_ids: extra, ...(dataset ? { dataset } : {}) },
      {
        onSuccess: (v) => {
          toast.success("Validation requested", "The checks run in the sandbox; this page updates when they finish.");
          void navigate({ to: "/validations/$validationId", params: { validationId: v.id } });
        },
      },
    );
  };
  return (
    <form className="grid gap-4" onSubmit={submit} aria-label="Request a validation">
      <div className="grid gap-2">
        <p className="text-[13px] font-medium">Policies it is held to</p>
        <p className="text-xs text-muted-foreground" data-testid="policies-in-force-note">
          Every policy in force for this kind of model always applies. Tick a policy to hold this version to it as well.
        </p>
        {items.length ? (
          <ul className="grid gap-1.5" aria-label="Also hold it to">
            {items.map((p) => {
              const checked = extra.includes(p.id);
              return (
                <li key={p.id} className="flex items-start gap-2 text-[13px]">
                  <Checkbox
                    id={`pol-${p.id}`}
                    checked={checked}
                    onCheckedChange={(c) => {
                      setExtra(c === true ? [...new Set([...extra, p.id])] : extra.filter((x) => x !== p.id));
                    }}
                  />
                  <label htmlFor={`pol-${p.id}`} className="grid">
                    <span className="font-medium">
                      {p.name} <span className="font-normal text-muted-foreground">v{p.version}</span>
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {p.scope === "tenant" ? "tenant-wide" : "this project"} · {p.checks.length} checks{p.require_approval ? " · needs a reviewer" : ""}
                      {p.require_validated_for_promotion ? " · gates promotion" : ""}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-[13px] text-muted-foreground">{policies.isPending ? "Loading the policies…" : "No policy is in force; the platform's defaults apply."}</p>
        )}
      </div>
      <DatasetRefPicker value={dataset} onChange={setDataset} label="Data to check against" required={false} hint="Leave empty to use the data the model was fitted on." />
      <Field label="Note (optional)" htmlFor="val-note">
        <Textarea id="val-note" maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Q4 refresh with the price driver" />
      </Field>
      {request.isError ? <ErrorNotice error={request.error} title="The validation was not requested" /> : null}
      <div>
        <Button type="submit" disabled={request.isPending}>
          {request.isPending ? <Spinner /> : <ShieldCheck />} Request validation
        </Button>
      </div>
    </form>
  );
}

export function ModelValidations({ modelId, version }: { modelId: string; version: number }) {
  const ws = useWorkspace();
  const model = useModel(modelId);
  const validations = useModelValidations(modelId, version);
  const mayValidate = can(ws.grants, "validate", "model");
  return (
    <div className="grid gap-5">
      <PageHeader
        title={`Validation of ${model.data?.name ?? shortId(modelId)} v${version}`}
        description="Run the governance checks on this version, and follow its approval."
        actions={
          <Button variant="outline" asChild>
            <Link to="/models/$modelId/versions/$version" params={{ modelId, version: String(version) }}>
              Back to the model
            </Link>
          </Button>
        }
      />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card>
          <CardHeader>
            <CardTitle>Validations</CardTitle>
          </CardHeader>
          <CardContent>
            <QueryView query={validations}>
              {(page) =>
                page.items.length ? (
                  <Table aria-label="Validations of this version">
                    <THead>
                      <TR>
                        <TH>Status</TH>
                        <TH>Submitted</TH>
                        <TH>Failing checks</TH>
                        <TH />
                      </TR>
                    </THead>
                    <TBody>
                      {page.items.map((v) => (
                        <TR key={v.id}>
                          <TD>
                            <ValidationBadge status={v.status} />
                          </TD>
                          <TD>{v.submitted_at ? relativeTime(v.submitted_at) : "—"}</TD>
                          <TD>{v.failing_checks?.length ? <Mono>{v.failing_checks.join(", ")}</Mono> : "—"}</TD>
                          <TD>
                            <Button variant="ghost" size="sm" asChild>
                              <Link to="/validations/$validationId" params={{ validationId: v.id }}>
                                Open
                              </Link>
                            </Button>
                          </TD>
                        </TR>
                      ))}
                    </TBody>
                  </Table>
                ) : (
                  <EmptyState icon={<ShieldCheck />} title="Not validated yet">
                    Request a validation to run the governance checks on this version.
                  </EmptyState>
                )
              }
            </QueryView>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Request a validation</CardTitle>
              <CardDescription>The checks run in the sandbox; a reviewer approves when a policy asks for one.</CardDescription>
            </div>
          </CardHeader>
          <CardContent>{mayValidate ? <RequestForm modelId={modelId} version={version} /> : <p className="text-[13px] text-muted-foreground">Requesting a validation needs the runner role.</p>}</CardContent>
        </Card>
      </div>
    </div>
  );
}

/** Shown by the promotion dialog when the gate refuses an unvalidated version. */
export function NotValidatedNotice({ modelId, version }: { modelId: string; version: number }) {
  const validations = useModelValidations(modelId, version);
  const latest = validations.data?.items[0];
  return (
    <div role="alert" className="grid gap-2 rounded-md border border-destructive/35 bg-destructive/6 p-3 text-[13px]" data-testid="not-validated">
      <p className="flex items-center gap-2 font-medium">
        <ShieldAlert className="size-4 text-destructive" aria-hidden /> A governance policy requires a validated model
      </p>
      <p className="text-muted-foreground">
        {latest ? (
          <>
            Its latest validation is <ValidationBadge status={latest.status} />. Promotion needs it validated; no override passes this rule.
          </>
        ) : (
          "This version has not been submitted for validation."
        )}
      </p>
      <div className="flex flex-wrap gap-2">
        {latest ? (
          <Button size="sm" variant="outline" asChild>
            <Link to="/validations/$validationId" params={{ validationId: latest.id }}>
              Open the validation
            </Link>
          </Button>
        ) : null}
        <Button size="sm" variant="outline" asChild>
          <Link to="/models/$modelId/versions/$version/validations" params={{ modelId, version: String(version) }}>
            {latest ? "All validations" : "Request a validation"}
          </Link>
        </Button>
      </div>
      <Badge variant="outline" className="justify-self-start">
        NOT_VALIDATED
      </Badge>
    </div>
  );
}
