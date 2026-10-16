/**
 * The policy editor: which models it covers, the required checks (each with its About card,
 * severity, thresholds and parameters from the check's JSON Schema), second-person approval
 * and the promotion requirement. Saving a policy stores a new version
 * (`expected_version` refuses a concurrent edit); versions can be compared.
 */
import { useNavigate } from "@tanstack/react-router";
import { ChevronDown, Plus, Save, Trash2 } from "lucide-react";
import { useId, useMemo, useState, type FormEvent } from "react";
import { stringify } from "yaml";

import { ApiError } from "@/api/client";
import { ErrorNotice, Mono, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { DiffView } from "@/features/editor/diff-view";
import { SchemaForm, type JsonObject, type SchemaNode } from "@/features/forms/json-schema-form";
import { relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

import { useGovernanceChecks, usePolicyMutations, usePolicyVersions } from "./api";
import { thresholdText } from "./evidence";
import { SEVERITY_META } from "./status";
import type { CheckSeverity, GovernanceCheckDef, GovernancePolicy, GovernancePolicyCreate, PolicyCheck, PolicyScope } from "./types";

export const KNOWN_FAMILIES = ["mmm", "ts", "forecast", "ts_multivariate", "regime", "tabular", "choice", "sklearn"];
const SEVERITIES: CheckSeverity[] = ["blocking", "warning", "info"];

/** `mapi_spec.governance.PolicyCheck` threshold rule: fail is at least as strict as warn. */
export function thresholdProblem(def: GovernanceCheckDef | undefined, warn: number, fail: number): string | null {
  if (!Number.isFinite(warn) || !Number.isFinite(fail)) return "Both limits must be numbers.";
  if (!def || warn === fail) return null;
  const lenient = fail >= warn;
  if ((def.direction === "min") === lenient) return def.direction === "min" ? "The fail limit must be at most the warn limit (higher is better)." : "The fail limit must be at least the warn limit (lower is better).";
  return null;
}

export function blankPolicy(): GovernancePolicyCreate {
  return { name: "", description: null, applies_to: { families: [], models: [], templates: [] }, checks: [], require_approval: false, approvals_required: 1, require_validated_for_promotion: true, active: true };
}

function toForm(p: GovernancePolicy): GovernancePolicyCreate {
  return {
    name: p.name,
    description: p.description ?? null,
    applies_to: { families: p.applies_to?.families ?? [], models: p.applies_to?.models ?? [], templates: p.applies_to?.templates ?? [] },
    checks: structuredClone(p.checks),
    require_approval: p.require_approval ?? false,
    approvals_required: p.approvals_required ?? 1,
    require_validated_for_promotion: p.require_validated_for_promotion ?? true,
    active: p.active ?? true,
  };
}

function ListInput({ id, label, hint, values, onChange, suggestions = [] }: { id: string; label: string; hint: string; values: string[]; onChange: (v: string[]) => void; suggestions?: string[] }) {
  const [draft, setDraft] = useState("");
  const add = (v: string) => {
    const t = v.trim();
    if (t && !values.includes(t)) onChange([...values, t]);
    setDraft("");
  };
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <div className="grid gap-2">
        {values.length ? (
          <ul className="flex flex-wrap gap-1" aria-label={label}>
            {values.map((v) => (
              <li key={v}>
                <Badge variant="secondary" className="gap-1 font-mono">
                  {v}
                  <button type="button" aria-label={`Remove ${v}`} className="rounded hover:text-destructive" onClick={() => onChange(values.filter((x) => x !== v))}>
                    ×
                  </button>
                </Badge>
              </li>
            ))}
          </ul>
        ) : null}
        <Input
          id={id}
          list={suggestions.length ? `${id}-list` : undefined}
          value={draft}
          placeholder={values.length ? "Add another" : "Empty: matches every model"}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add(draft);
            }
          }}
          onBlur={() => draft && add(draft)}
        />
        {suggestions.length ? (
          <datalist id={`${id}-list`}>
            {suggestions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        ) : null}
      </div>
    </Field>
  );
}

function CheckRow({ check, def, onChange, onRemove, readOnly, error }: { check: PolicyCheck; def: GovernanceCheckDef | undefined; onChange: (c: PolicyCheck) => void; onRemove: () => void; readOnly: boolean; error?: string | undefined }) {
  const id = useId();
  const custom = Boolean(check.thresholds);
  const warn = check.thresholds?.warn ?? def?.warn ?? 0;
  const fail = check.thresholds?.fail ?? def?.fail ?? 0;
  const tProblem = custom ? thresholdProblem(def, warn, fail) : null;
  return (
    <li className="grid gap-3 rounded-lg border p-3" data-check={check.check}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium">{def?.title ?? check.check}</p>
          <Mono className="text-muted-foreground">{check.check}</Mono>
          {def ? <p className="mt-1 text-[13px] text-muted-foreground">{def.about.summary_plain}</p> : null}
        </div>
        {readOnly ? null : (
          <Button variant="ghost" size="icon-sm" aria-label={`Remove ${def?.title ?? check.check}`} onClick={onRemove}>
            <Trash2 />
          </Button>
        )}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Severity" htmlFor={`${id}-sev`} hint={SEVERITY_META[check.severity ?? "blocking"].plain}>
          <NativeSelect id={`${id}-sev`} disabled={readOnly} value={check.severity ?? "blocking"} onChange={(e) => onChange({ ...check, severity: e.target.value as CheckSeverity })}>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {SEVERITY_META[s].label}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <div className="grid gap-1.5">
          <span className="text-[13px] font-medium">Limits</span>
          <label className="flex items-center gap-2 text-[13px]">
            <Switch
              checked={custom}
              disabled={readOnly}
              onCheckedChange={(on) => {
                const next = { ...check };
                if (on) next.thresholds = { warn: def?.warn ?? 0, fail: def?.fail ?? 0 };
                else delete next.thresholds;
                onChange(next);
              }}
              aria-label={`Custom limits for ${check.check}`}
            />
            {custom ? "Custom" : `Catalog default: ${thresholdText(def, def?.warn) ?? "—"}`}
          </label>
        </div>
      </div>
      {custom ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={`Warn ${def?.direction === "min" ? "below" : "above"}`} htmlFor={`${id}-warn`} error={tProblem}>
            <Input id={`${id}-warn`} type="number" step="any" disabled={readOnly} value={warn} onChange={(e) => onChange({ ...check, thresholds: { warn: Number(e.target.value), fail } })} />
          </Field>
          <Field label={`Fail ${def?.direction === "min" ? "below" : "above"}`} htmlFor={`${id}-fail`} hint={def ? `Measured as ${def.metric.replace(/_/g, " ")}` : undefined}>
            <Input id={`${id}-fail`} type="number" step="any" disabled={readOnly} value={fail} onChange={(e) => onChange({ ...check, thresholds: { warn, fail: Number(e.target.value) } })} />
          </Field>
        </div>
      ) : null}
      {def && Object.keys((def.params_schema as SchemaNode).properties ?? {}).length ? (
        <details>
          <summary className="cursor-pointer text-[13px] font-medium">Settings</summary>
          <div className="mt-3">
            <SchemaForm schema={def.params_schema as SchemaNode} value={(check.params ?? {}) as JsonObject} onChange={(params) => onChange({ ...check, params })} disabled={readOnly} />
          </div>
        </details>
      ) : null}
      {def ? (
        <details>
          <summary className="flex cursor-pointer items-center gap-1 text-xs text-muted-foreground">
            <ChevronDown className="size-3" aria-hidden /> About this check
          </summary>
          <AboutCard about={def.about} className="mt-2" headingLevel={4} />
        </details>
      ) : null}
      {error ? (
        <p role="alert" className="text-xs font-medium text-destructive">
          {error}
        </p>
      ) : null}
    </li>
  );
}

export function PolicyEditor({ scope, policy, readOnly }: { scope: PolicyScope; policy: GovernancePolicy | null; readOnly: boolean }) {
  const navigate = useNavigate();
  const checks = useGovernanceChecks();
  const { create, update, retire } = usePolicyMutations(scope);
  const [form, setForm] = useState<GovernancePolicyCreate>(() => (policy ? toForm(policy) : blankPolicy()));
  const [adding, setAdding] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const id = useId();
  const used = new Set(form.checks.map((c) => c.check));
  const candidate = adding ? checks.map.get(adding) : undefined;
  const thresholdErrors = form.checks.map((c) => (c.thresholds ? thresholdProblem(checks.map.get(c.check), c.thresholds.warn, c.thresholds.fail) : null));
  const nameError = !form.name.trim() ? "Give the policy a name." : null;
  const checksError = form.checks.length ? null : "Add at least one check.";
  const busy = create.isPending || update.isPending;
  const saveError = create.error ?? update.error;
  const serverPointer = saveError instanceof ApiError ? (saveError.problem?.errors ?? []) : [];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (nameError || checksError || thresholdErrors.some(Boolean) || readOnly) return;
    if (policy) {
      update.mutate(
        { id: policy.id, body: { ...form, expected_version: policy.version } },
        {
          onSuccess: (p) => {
            toast.success(`Saved version ${p.version}`, "Validations from now on are held to this version.");
            void navigate({ to: "/governance/policies/$policyId", params: { policyId: p.id }, search: scope === "tenant" ? { scope: "tenant" } : {} });
          },
        },
      );
    } else {
      create.mutate(form, {
        onSuccess: (p) => {
          toast.success("Policy created", p.name);
          void navigate({ to: "/governance/policies/$policyId", params: { policyId: p.id }, search: scope === "tenant" ? { scope: "tenant" } : {} });
        },
      });
    }
  };

  return (
    <form className="grid gap-5" onSubmit={submit} noValidate data-testid="policy-editor">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Policy</CardTitle>
            <CardDescription>{scope === "tenant" ? "Tenant-wide: every project inherits it as a floor (a project can tighten it, never loosen it)." : "Applies to this project's models, on top of the tenant-wide policies."}</CardDescription>
          </div>
          {policy ? <Badge variant="outline">version {policy.version}</Badge> : null}
        </CardHeader>
        <CardContent className="grid gap-4">
          <Field label="Name" htmlFor={`${id}-name`} error={submitted ? nameError : null}>
            <Input id={`${id}-name`} disabled={readOnly} maxLength={120} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Forecasts we publish" />
          </Field>
          <Field label="Description (optional)" htmlFor={`${id}-desc`}>
            <Textarea id={`${id}-desc`} disabled={readOnly} maxLength={4000} value={form.description ?? ""} onChange={(e) => setForm({ ...form, description: e.target.value || null })} />
          </Field>
          <div className="grid gap-4 md:grid-cols-3">
            <ListInput id={`${id}-fam`} label="Model families" hint="mmm, ts, tabular…" suggestions={KNOWN_FAMILIES} values={form.applies_to?.families ?? []} onChange={(families) => setForm({ ...form, applies_to: { ...form.applies_to, families } })} />
            <ListInput id={`${id}-mod`} label="Model ids" hint="e.g. ts.sarimax" values={form.applies_to?.models ?? []} onChange={(models) => setForm({ ...form, applies_to: { ...form.applies_to, models } })} />
            <ListInput id={`${id}-tpl`} label="Guided templates" hint="e.g. geo_mmm" values={form.applies_to?.templates ?? []} onChange={(templates) => setForm({ ...form, applies_to: { ...form.applies_to, templates } })} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div>
            <CardTitle>Required checks</CardTitle>
            <CardDescription>A blocking check that fails (or cannot be computed) stops validation; warnings are shown to the reviewer.</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="grid gap-3">
          <ul className="grid gap-3" aria-label="Required checks">
            {form.checks.map((c, i) => (
              <CheckRow
                key={c.check}
                check={c}
                def={checks.map.get(c.check)}
                readOnly={readOnly}
                error={serverPointer.find((x) => x.pointer?.startsWith(`/checks/${i}`))?.message}
                onChange={(next) => setForm({ ...form, checks: form.checks.map((x, j) => (j === i ? next : x)) })}
                onRemove={() => setForm({ ...form, checks: form.checks.filter((_, j) => j !== i) })}
              />
            ))}
          </ul>
          {submitted && checksError ? (
            <p role="alert" className="text-[13px] text-destructive">
              {checksError}
            </p>
          ) : null}
          {readOnly ? null : (
            <div className="grid gap-3 rounded-lg border border-dashed p-3">
              <Field label="Add a check" htmlFor={`${id}-add`}>
                <NativeSelect id={`${id}-add`} value={adding} onChange={(e) => setAdding(e.target.value)}>
                  <option value="">Choose from the catalog</option>
                  {(checks.data?.items ?? [])
                    .filter((c) => !used.has(c.id))
                    .map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.title} ({c.id})
                      </option>
                    ))}
                </NativeSelect>
              </Field>
              {candidate ? (
                <AboutCard
                  about={candidate.about}
                  headingLevel={4}
                  actions={
                    <Button
                      size="sm"
                      onClick={() => {
                        setForm({ ...form, checks: [...form.checks, { check: candidate.id, severity: candidate.default_severity ?? "blocking", params: {} }] });
                        setAdding("");
                      }}
                    >
                      <Plus /> Add check
                    </Button>
                  }
                />
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sign-off and promotion</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4">
          <label className="flex items-start gap-3 text-[13px]">
            <Switch checked={Boolean(form.require_approval)} disabled={readOnly} onCheckedChange={(v) => setForm({ ...form, require_approval: v })} aria-label="Require approval by a reviewer" />
            <span>
              <span className="font-medium">Require approval by a reviewer</span>
              <span className="block text-muted-foreground">Someone other than the submitter, with the reviewer capability, approves each validation. API tokens can never approve.</span>
            </span>
          </label>
          {form.require_approval ? (
            <Field label="Approvals needed" htmlFor={`${id}-n`}>
              <NativeSelect id={`${id}-n`} className="w-40" disabled={readOnly} value={form.approvals_required ?? 1} onChange={(e) => setForm({ ...form, approvals_required: Number(e.target.value) })}>
                {[1, 2, 3].map((n) => (
                  <option key={n} value={n}>
                    {n} reviewer{n === 1 ? "" : "s"}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          ) : null}
          <label className="flex items-start gap-3 text-[13px]">
            <Switch checked={Boolean(form.require_validated_for_promotion)} disabled={readOnly} onCheckedChange={(v) => setForm({ ...form, require_validated_for_promotion: v })} aria-label="Only validated models can be promoted" />
            <span>
              <span className="font-medium">Only validated models can be promoted to an endpoint</span>
              <span className="block text-muted-foreground">The promotion gate refuses other versions (NOT_VALIDATED); no administrator override passes it.</span>
            </span>
          </label>
          <label className="flex items-start gap-3 text-[13px]">
            <Switch checked={form.active !== false} disabled={readOnly} onCheckedChange={(v) => setForm({ ...form, active: v })} aria-label="Policy active" />
            <span>
              <span className="font-medium">Active</span>
              <span className="block text-muted-foreground">An inactive policy is kept but no longer applies.</span>
            </span>
          </label>
        </CardContent>
      </Card>

      {saveError ? (
        saveError instanceof ApiError && saveError.code === "CONFLICT" ? (
          <p role="alert" className="text-[13px] text-warning">
            Someone saved a newer version of this policy meanwhile. Reload the page to see it, then make your change again.
          </p>
        ) : (
          <ErrorNotice error={saveError} title="The policy was not saved" />
        )
      ) : null}
      {readOnly ? (
        <p className="text-[13px] text-muted-foreground">{scope === "tenant" ? "Only tenant admins change tenant-wide policies." : "Only project admins change policies."}</p>
      ) : (
        <div className="flex flex-wrap justify-between gap-2">
          {policy ? (
            <Button
              variant="outline"
              className="text-destructive"
              onClick={() => {
                if (!window.confirm(`Retire “${policy.name}”? Its versions are kept for audit.`)) return;
                retire.mutate(policy.id, {
                  onSuccess: () => {
                    toast.success("Policy retired");
                    void navigate({ to: "/governance" });
                  },
                });
              }}
            >
              <Trash2 /> Retire policy
            </Button>
          ) : (
            <span />
          )}
          <Button type="submit" disabled={busy}>
            {busy ? <Spinner /> : <Save />} {policy ? `Save version ${policy.version + 1}` : "Create policy"}
          </Button>
        </div>
      )}
    </form>
  );
}

/** Every version of a policy, newest first, and a diff between two. */
export function PolicyVersions({ scope, policyId }: { scope: PolicyScope; policyId: string }) {
  const versions = usePolicyVersions(scope, policyId);
  const items = useMemo(() => versions.data?.items ?? [], [versions.data]);
  const [pair, setPair] = useState<[number, number] | null>(null);
  const yamlOf = (v: number) => {
    const p = items.find((x) => x.version === v);
    if (!p) return "";
    const { name, description, applies_to, checks, require_approval, approvals_required, require_validated_for_promotion, active } = p;
    return stringify({ name, description, applies_to, checks, require_approval, approvals_required, require_validated_for_promotion, active }, { lineWidth: 0 });
  };
  if (!items.length) return null;
  return (
    <Card data-testid="policy-versions">
      <CardHeader>
        <CardTitle>Versions</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <ol className="grid gap-1">
          {items.map((v, i) => (
            <li key={v.version} className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
              <span>
                <span className="font-mono">v{v.version}</span> · {v.checks.length} checks{v.require_approval ? " · approval" : ""}
                {v.active === false ? " · retired" : ""} · <span title={v.created_at}>{relativeTime(v.created_at)}</span>
              </span>
              {items[i + 1] ? (
                <Button variant="ghost" size="sm" onClick={() => setPair([items[i + 1]!.version, v.version])}>
                  Compare with v{items[i + 1]!.version}
                </Button>
              ) : null}
            </li>
          ))}
        </ol>
        {pair ? <DiffView original={yamlOf(pair[0])} modified={yamlOf(pair[1])} height="40vh" /> : null}
      </CardContent>
    </Card>
  );
}
