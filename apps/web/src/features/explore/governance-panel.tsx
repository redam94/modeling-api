/**
 * Governance evidence of a model version in the Model Explorer (read-only): the latest
 * validation's status, the checks it was held to and their results with evidence
 * (`GET /v1/models/{model_id}/versions/{version}/validations`).
 */
import { AlertTriangle, CheckCircle2, CircleSlash, ShieldCheck, ShieldQuestion, XCircle } from "lucide-react";

import { EmptyState, ErrorNotice, JsonBlock, LoadingRows } from "@/components/common";
import { Badge } from "@/components/ui/primitives";
import { useAbout } from "@/features/about/api";
import { AboutButton } from "@/features/about/about-card";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { useModelValidations, type CheckResult, type EvidenceItem, type ModelValidation, type ValidationStatus } from "./api";
import { num, StatTable } from "./ui";

export const VALIDATION_META: Record<ValidationStatus, { label: string; variant: "success" | "warning" | "danger" | "info" | "outline"; plain: string }> = {
  draft: { label: "Not validated", variant: "outline", plain: "No checks have run on this version yet." },
  checks_running: { label: "Checks running", variant: "info", plain: "The governance checks are running in the sandbox." },
  checks_passed: { label: "Checks passed", variant: "success", plain: "Every required check passed." },
  checks_failed: { label: "Checks failed", variant: "danger", plain: "A required check failed: fix the model or the data and resubmit." },
  awaiting_approval: { label: "Awaiting approval", variant: "warning", plain: "The checks passed; a reviewer has to approve it." },
  validated: { label: "Validated", variant: "success", plain: "The version passed its checks and may be promoted." },
  rejected: { label: "Rejected", variant: "danger", plain: "A reviewer rejected this version." },
  deprecated: { label: "Deprecated", variant: "outline", plain: "The version was validated, then retired." },
};

/** The latest validation (by update time). */
export function latestValidation(items: readonly ModelValidation[] | undefined): ModelValidation | null {
  return [...(items ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0] ?? null;
}

export function ValidationBadge({ status, className }: { status: ValidationStatus | null; className?: string }) {
  if (!status) {
    return (
      <Badge variant="outline" className={className}>
        <ShieldQuestion aria-hidden /> Not validated
      </Badge>
    );
  }
  const meta = VALIDATION_META[status];
  return (
    <Badge variant={meta.variant} className={className} title={meta.plain} data-validation={status}>
      {status === "validated" ? <ShieldCheck aria-hidden /> : null} {meta.label}
    </Badge>
  );
}

/** The validation status of a version, for headers. */
export function ValidationStatusBadge({ modelId, version }: { modelId: string; version: number }) {
  const v = useModelValidations(modelId, version);
  if (v.isPending || v.isError) return null;
  return <ValidationBadge status={latestValidation(v.data.items)?.status ?? null} />;
}

const STATUS_ICON = {
  pass: { icon: CheckCircle2, className: "text-success", label: "Pass" },
  warn: { icon: AlertTriangle, className: "text-warning", label: "Warning" },
  fail: { icon: XCircle, className: "text-destructive", label: "Fail" },
  error: { icon: XCircle, className: "text-destructive", label: "Could not run" },
  skipped: { icon: CircleSlash, className: "text-muted-foreground", label: "Does not apply" },
} as const;

/** A table cell of evidence (numbers formatted, other values as JSON text). */
function cellText(v: unknown): string {
  if (typeof v === "number") return num(v);
  if (typeof v === "string") return v;
  if (v === null || v === undefined) return "—";
  return JSON.stringify(v);
}

function Evidence({ item }: { item: EvidenceItem }) {
  const inline = item.inline;
  const records = Array.isArray(inline) && inline.every((r) => typeof r === "object" && r !== null && !Array.isArray(r)) ? (inline as Record<string, unknown>[]) : null;
  return (
    <div className="grid gap-1 text-[13px]">
      <p className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-xs">{item.name}</span>
        {item.value != null ? <span className="font-medium tabular">{num(item.value)}</span> : null}
      </p>
      {item.caption_plain ? <p className="text-xs text-muted-foreground">{item.caption_plain}</p> : null}
      {records && records.length ? (
        <StatTable
          label={item.name}
          rows={records.slice(0, 50)}
          rowKey={(_, i) => String(i)}
          maxHeight="14rem"
          columns={Object.keys(records[0] ?? {}).map((k) => ({
            key: k,
            header: k,
            numeric: typeof records[0]?.[k] === "number",
            cell: (r: Record<string, unknown>) => cellText(r[k]),
          }))}
        />
      ) : inline !== undefined && inline !== null && !records ? (
        typeof inline === "string" ? <p>{inline}</p> : <JsonBlock value={inline} maxHeight="12rem" />
      ) : null}
    </div>
  );
}

function CheckRow({ result, expert }: { result: CheckResult; expert: boolean }) {
  const { about } = useAbout("check", result.check);
  const meta = STATUS_ICON[result.status];
  const Icon = meta.icon;
  return (
    <li className="grid grid-cols-[1.25rem_1fr] gap-3 rounded-lg border p-3" data-check={result.check} data-status={result.status}>
      <Icon aria-hidden className={cn("mt-0.5 size-4", meta.className)} />
      <div className="grid min-w-0 gap-1.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="flex items-center gap-1 font-medium">
            {about?.title ?? result.check}
            <AboutButton kind="check" id={result.check} />
          </p>
          <span className="flex flex-wrap items-center gap-1.5">
            <span className={cn("text-xs font-medium", meta.className)}>{meta.label}</span>
            <Badge variant="outline">{result.severity}</Badge>
          </span>
        </div>
        <p className="text-[13px]">{result.message_plain}</p>
        {result.suggested_action && result.status !== "pass" ? <p className="text-[13px] text-muted-foreground">{result.suggested_action}</p> : null}
        {result.value != null ? (
          <p className="text-xs text-muted-foreground tabular">
            Measured {num(result.value)}
            {result.threshold != null ? ` · limit ${num(result.threshold)}` : ""}
            {result.metric ? ` · ${result.metric}` : ""}
          </p>
        ) : null}
        {(result.evidence ?? []).length ? (
          <details open={expert}>
            <summary className="cursor-pointer text-xs text-muted-foreground">Evidence ({(result.evidence ?? []).length})</summary>
            <div className="mt-2 grid gap-3">
              {(result.evidence ?? []).map((e, i) => (
                <Evidence key={`${e.name}-${i}`} item={e} />
              ))}
            </div>
          </details>
        ) : null}
        {expert && result.message_expert ? <p className="font-mono text-[11px] text-muted-foreground">{result.message_expert}</p> : null}
      </div>
    </li>
  );
}

export function GovernancePanel({ modelId, version, expert }: { modelId: string; version: number; expert: boolean }) {
  const v = useModelValidations(modelId, version);
  if (v.isPending) return <LoadingRows rows={4} />;
  if (v.isError) return <ErrorNotice error={v.error} title="The validations could not be loaded" />;
  const latest = latestValidation(v.data.items);
  if (!latest) {
    return (
      <EmptyState icon={<ShieldQuestion />} title="Not validated yet">
        No governance checks have run on this version. A validation runs the checks of your project&apos;s policies (null tests against shuffled data, benchmarks, residual whiteness, convergence and more) in the sandbox and records the evidence here.
      </EmptyState>
    );
  }
  const meta = VALIDATION_META[latest.status];
  const results = latest.results ?? [];
  const order = { fail: 0, error: 1, warn: 2, pass: 3, skipped: 4 } as const;
  const sorted = [...results].sort((a, b) => order[a.status] - order[b.status]);
  const required = new Map((latest.required_checks ?? []).map((c) => [c.check, c]));
  return (
    <div className="grid gap-4">
      <div className={cn("grid gap-1.5 rounded-lg border-l-[3px] bg-muted/30 px-4 py-3", meta.variant === "success" ? "border-l-success" : meta.variant === "danger" ? "border-l-destructive" : "border-l-warning")}>
        <p className="flex flex-wrap items-center gap-2">
          <ValidationBadge status={latest.status} />
          <span className="text-[14px]">{meta.plain}</span>
        </p>
        <p className="text-xs text-muted-foreground">
          {results.filter((r) => r.status === "pass").length} of {results.length} checks passed
          {latest.submitted_at ? ` · submitted ${relativeTime(latest.submitted_at)}` : ""}
          {latest.require_approval ? ` · needs ${latest.approvals_required ?? 1} approval${(latest.approvals_required ?? 1) === 1 ? "" : "s"}` : ""}
          {latest.policies?.length ? ` · policies ${latest.policies.join(", ")}` : ""}
        </p>
        {latest.failing_checks?.length ? <p className="text-[13px]">Blocking: {latest.failing_checks.join(", ")}</p> : null}
      </div>
      <ul className="grid gap-2" aria-label="Check results">
        {sorted.map((r) => (
          <CheckRow key={r.check} result={{ ...r, severity: required.get(r.check)?.severity ?? r.severity }} expert={expert} />
        ))}
      </ul>
      {expert && (latest.history ?? []).length ? (
        <StatTable
          label="Status history"
          rows={latest.history ?? []}
          rowKey={(h, i) => `${h.at}-${i}`}
          columns={[
            { key: "at", header: "When", cell: (h) => relativeTime(h.at) },
            { key: "from", header: "From", cell: (h) => h.from_status },
            { key: "to", header: "To", cell: (h) => h.to_status },
            { key: "by", header: "By", cell: (h) => h.actor_id ?? "—" },
            { key: "note", header: "Note", cell: (h) => h.note ?? "" },
          ]}
        />
      ) : null}
    </div>
  );
}
