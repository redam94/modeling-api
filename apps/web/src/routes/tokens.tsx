/**
 * API tokens of the selected project (`/v1/tokens`). A token acts as its owner, narrowed by
 * its scopes, in this project only. The secret exists in this page's memory exactly once,
 * right after minting, and is dropped when the dialog closes.
 */
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, KeyRound, Plus, ShieldAlert, Trash2 } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { atLeast, can, DEFAULT_TOKEN_TTL_DAYS, MAX_TOKEN_TTL_DAYS } from "@/api/access";
import { errorMessage } from "@/api/client";
import { useCreateToken, useRevokeToken, useTokens } from "@/api/queries";
import type { ApiToken, BudgetPeriod, ProjectRole, TokenCreate, TokenCreated, TokenStatus } from "@/api/types";
import { CopyButton, EmptyState, ErrorNotice, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Switch } from "@/components/ui/overlays";
import { Badge, Field, Input, Label, NativeSelect } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { env } from "@/env";
import { formatVcpuSeconds } from "@/lib/cost";
import { absoluteTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

const route = getRouteApi("/tokens");

interface ScopeOption {
  value: string;
  label: string;
  description: string;
  /** The project role the scope needs to have any effect. */
  needs: ProjectRole;
}

/** `mapi_spec.tokens.scope_allows`, in words. */
export const SCOPE_OPTIONS: ScopeOption[] = [
  { value: "models:read", label: "Read", description: "Datasets, specs, runs, models, plots and usage.", needs: "viewer" },
  { value: "runs:create", label: "Run", description: "Read, plus start and cancel runs and score models.", needs: "runner" },
  { value: "datasets:write", label: "Write datasets", description: "Upload data and create dataset versions.", needs: "editor" },
  { value: "plugins:publish", label: "Publish plugins", description: "Publish and yank plugin versions.", needs: "admin" },
  { value: "admin", label: "Full access", description: "Everything your role allows in this project.", needs: "viewer" },
];

const EXPIRY_CHOICES = [7, 30, 90, 180, 365].filter((d) => d <= MAX_TOKEN_TTL_DAYS);
const ALIAS_RE = /^(?:\*|[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?)$/;
const NAME_RE = /^[^\u0000-\u001f\u007f]+$/;

const STATUS_VARIANT: Record<TokenStatus, "success" | "outline" | "danger"> = {
  active: "success",
  expired: "outline",
  revoked: "danger",
};

function soon(iso: string, days: number): boolean {
  const t = Date.parse(iso);
  return !Number.isNaN(t) && t - Date.now() < days * 86_400_000 && t > Date.now();
}

export function TokensPage() {
  const ws = useWorkspace();
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/tokens" });
  const canManage = can(ws.grants, "manage", "token");
  const canCreate = can(ws.grants, "create", "token");
  const filters = useMemo(() => ({ ...(search.all && canManage ? { all: true } : {}), ...(search.status ? { status: search.status } : {}) }), [search.all, search.status, canManage]);
  const tokens = useTokens(filters);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<TokenCreated | null>(null);
  const [revoking, setRevoking] = useState<ApiToken | null>(null);
  const project = ws.current;

  const columns = useMemo<ColumnDef<ApiToken, unknown>[]>(
    () => [
      {
        accessorKey: "name",
        header: "Token",
        cell: ({ row }) => (
          <div className="grid min-w-0">
            <span className="truncate font-medium">{row.original.name}</span>
            <Mono className="text-muted-foreground">{row.original.display}</Mono>
          </div>
        ),
      },
      {
        id: "scopes",
        header: "Scopes",
        accessorFn: (t) => t.scopes.join(" "),
        cell: ({ row }) => (
          <div className="flex max-w-72 flex-wrap gap-1">
            {row.original.scopes.map((s) => (
              <Badge key={s} variant={s === "admin" ? "warning" : "outline"} className="font-mono text-[11px]">
                {s}
              </Badge>
            ))}
          </div>
        ),
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ row }) => <Badge variant={STATUS_VARIANT[row.original.status]}>{row.original.status}</Badge>,
      },
      {
        id: "limits",
        header: "Limits",
        accessorFn: (t) => t.rate_limit_per_minute ?? 0,
        cell: ({ row }) => {
          const t = row.original;
          const parts = [
            t.rate_limit_per_minute ? `${t.rate_limit_per_minute.toLocaleString("en")}/min` : null,
            t.vcpu_seconds_budget ? `${formatVcpuSeconds(t.vcpu_seconds_budget)} per ${t.budget_period ?? "month"}` : null,
          ].filter(Boolean);
          return <span className="text-muted-foreground">{parts.length ? parts.join(" · ") : "Project defaults"}</span>;
        },
      },
      {
        accessorKey: "last_used_at",
        header: "Last used",
        sortingFn: "datetime",
        cell: ({ row }) => (
          <span title={absoluteTime(row.original.last_used_at)} className={cn(!row.original.last_used_at && "text-muted-foreground")}>
            {row.original.last_used_at ? relativeTime(row.original.last_used_at) : "Never"}
          </span>
        ),
      },
      {
        accessorKey: "expires_at",
        header: "Expires",
        cell: ({ row }) => {
          const t = row.original;
          if (t.status === "revoked") return <span className="text-muted-foreground">Revoked {relativeTime(t.revoked_at)}</span>;
          const warn = t.status === "active" && soon(t.expires_at, 7);
          return (
            <span title={absoluteTime(t.expires_at)} className={cn("inline-flex items-center gap-1", warn && "text-warning")}>
              {warn ? <AlertTriangle className="size-3.5" aria-label="Expires within a week" /> : null}
              {relativeTime(t.expires_at)}
            </span>
          );
        },
      },
      ...(filters.all
        ? [{ accessorKey: "owner_id", header: "Owner", cell: ({ row }) => <Mono>{row.original.owner_id}</Mono> } satisfies ColumnDef<ApiToken, unknown>]
        : []),
      {
        id: "actions",
        header: () => <span className="sr-only">Actions</span>,
        enableSorting: false,
        cell: ({ row }) =>
          row.original.status === "active" && (canManage || row.original.owner_id === ws.user.id) ? (
            <Button variant="ghost" size="sm" onClick={() => setRevoking(row.original)} aria-label={`Revoke ${row.original.name}`}>
              <Trash2 /> Revoke
            </Button>
          ) : null,
      },
    ],
    [filters.all, canManage, ws.user.id],
  );

  return (
    <>
      <PageHeader
        title="API tokens"
        description={
          <>
            Tokens let programs, notebooks and integrations act as you in <span className="font-medium text-foreground">{project?.project.name ?? "this project"}</span>, limited to the scopes you choose. The secret is shown once.
          </>
        }
        actions={
          <>
            {canManage ? (
              <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
                <Switch
                  checked={Boolean(search.all)}
                  onCheckedChange={(v) => void navigate({ search: (s) => ({ ...s, all: v ? true : undefined }) })}
                  aria-label="Show everyone's tokens"
                />
                Everyone&apos;s tokens
              </label>
            ) : null}
            <NativeSelect
              aria-label="Filter by status"
              className="h-8 w-36"
              value={search.status ?? ""}
              onChange={(e) => void navigate({ search: (s) => ({ ...s, status: (e.target.value || undefined) as TokenStatus | undefined }) })}
            >
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="expired">Expired</option>
              <option value="revoked">Revoked</option>
            </NativeSelect>
            <Button onClick={() => setCreating(true)} disabled={!canCreate}>
              <Plus /> New token
            </Button>
          </>
        }
      />
      <QueryView query={tokens}>
        {(page) =>
          page.items.length === 0 && !search.status && !search.all ? (
            <EmptyState
              icon={<KeyRound />}
              title="No tokens yet"
              action={
                canCreate ? (
                  <Button onClick={() => setCreating(true)}>
                    <Plus /> Create your first token
                  </Button>
                ) : null
              }
            >
              Create a token to call the API from a notebook, the <Mono>mapi</Mono> CLI, Atlas or another service. Use the narrowest scopes that work, and give it an expiry.
            </EmptyState>
          ) : (
            <DataTable label="API tokens" data={page.items} columns={columns} getRowId={(t) => t.id} empty="No tokens match these filters." filterPlaceholder="Filter tokens" />
          )
        }
      </QueryView>
      {creating ? (
        <CreateTokenDialog
          role={project?.role ?? null}
          onClose={() => setCreating(false)}
          onCreated={(t) => {
            setCreating(false);
            setCreated(t);
          }}
        />
      ) : null}
      {created ? <SecretDialog created={created} onClose={() => setCreated(null)} /> : null}
      {revoking ? <RevokeDialog token={revoking} onClose={() => setRevoking(null)} /> : null}
    </>
  );
}

interface TokenForm {
  name: string;
  scopes: string[];
  aliases: string;
  expires: number;
  rate: string;
  budget: string;
  period: BudgetPeriod;
}

export function validateTokenForm(f: TokenForm): { body: TokenCreate | null; errors: Partial<Record<keyof TokenForm, string>> } {
  const errors: Partial<Record<keyof TokenForm, string>> = {};
  const name = f.name.trim();
  if (!name) errors.name = "Give the token a name you will recognise later.";
  else if (name.length > 100 || !NAME_RE.test(name)) errors.name = "Up to 100 characters, no control characters.";
  const aliases = f.aliases
    .split(/[\s,]+/)
    .map((a) => a.trim())
    .filter(Boolean);
  const badAlias = aliases.find((a) => !ALIAS_RE.test(a));
  if (badAlias) errors.aliases = `“${badAlias.slice(0, 40)}” is not an endpoint alias (lowercase letters, digits, - and _, or *).`;
  const scopes = [...new Set([...f.scopes, ...aliases.map((a) => `score:${a}`)])];
  if (!scopes.length) errors.scopes = "Pick at least one scope.";
  if (scopes.length > 32) errors.scopes = "At most 32 scopes.";
  let rate: number | null = null;
  if (f.rate.trim()) {
    rate = Number(f.rate);
    if (!Number.isInteger(rate) || rate < 1 || rate > 100_000) errors.rate = "A whole number from 1 to 100,000.";
  }
  let budget: number | null = null;
  if (f.budget.trim()) {
    budget = Number(f.budget);
    if (!Number.isFinite(budget) || budget <= 0) errors.budget = "A positive number of vCPU-seconds.";
  }
  if (!Number.isInteger(f.expires) || f.expires < 1 || f.expires > MAX_TOKEN_TTL_DAYS) errors.expires = `From 1 to ${MAX_TOKEN_TTL_DAYS} days.`;
  if (Object.keys(errors).length) return { body: null, errors };
  return {
    body: {
      name,
      scopes,
      expires_in_days: f.expires,
      ...(rate !== null ? { rate_limit_per_minute: rate } : {}),
      ...(budget !== null ? { vcpu_seconds_budget: budget, budget_period: f.period } : {}),
    },
    errors,
  };
}

function CreateTokenDialog({ role, onClose, onCreated }: { role: ProjectRole | null; onClose: () => void; onCreated: (t: TokenCreated) => void }) {
  const create = useCreateToken();
  const [form, setForm] = useState<TokenForm>({ name: "", scopes: ["models:read"], aliases: "", expires: DEFAULT_TOKEN_TTL_DAYS, rate: "", budget: "", period: "month" });
  const [submitted, setSubmitted] = useState(false);
  const { body, errors } = validateTokenForm(form);
  const shown = submitted ? errors : {};
  const set = <K extends keyof TokenForm>(k: K, v: TokenForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const toggle = (scope: string, on: boolean) => set("scopes", on ? [...form.scopes, scope] : form.scopes.filter((s) => s !== scope));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!body) return;
    create.mutate(body, { onSuccess: onCreated });
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>New API token</DialogTitle>
        <DialogDescription>
          The token can never do more than your role{role ? ` (${role})` : ""} allows; scopes narrow it further.
        </DialogDescription>
        <form className="grid gap-5" onSubmit={submit} noValidate>
          <Field label="Name" htmlFor="tok-name" error={shown.name} hint="For example the program that will use it: atlas-optimiser, nightly-refit.">
            <Input id="tok-name" autoFocus maxLength={100} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <fieldset className="grid gap-2" aria-describedby={shown.scopes ? "tok-scopes-error" : undefined}>
            <legend className="mb-1 text-[13px] font-medium">Scopes</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {SCOPE_OPTIONS.map((o) => {
                const checked = form.scopes.includes(o.value);
                const ineffective = role !== null && !atLeast(role, o.needs);
                const id = `scope-${o.value.replace(/[^a-z]/g, "-")}`;
                return (
                  <label
                    key={o.value}
                    htmlFor={id}
                    className={cn("flex cursor-pointer items-start gap-3 rounded-lg border p-3 hover:bg-accent/40", checked && "border-primary/50 bg-accent/40")}
                  >
                    <Checkbox id={id} checked={checked} onCheckedChange={(v) => toggle(o.value, v === true)} className="mt-0.5" />
                    <span className="grid gap-0.5">
                      <span className="flex items-center gap-2 text-[13px] font-medium">
                        {o.label} <Mono className="text-muted-foreground">{o.value}</Mono>
                      </span>
                      <span className="text-xs text-muted-foreground">{o.description}</span>
                      {ineffective ? <span className="text-xs text-warning">Needs the {o.needs} role; it has no effect for you.</span> : null}
                    </span>
                  </label>
                );
              })}
            </div>
            {form.scopes.includes("admin") ? (
              <p className="flex items-start gap-2 text-xs text-warning">
                <ShieldAlert className="mt-0.5 size-3.5 shrink-0" /> Full access acts with your whole role. Prefer narrower scopes for programs.
              </p>
            ) : null}
            {shown.scopes ? (
              <p id="tok-scopes-error" role="alert" className="text-xs font-medium text-destructive">
                {shown.scopes}
              </p>
            ) : null}
          </fieldset>
          <Field
            label="Score endpoints (optional)"
            htmlFor="tok-aliases"
            error={shown.aliases}
            hint="Endpoint aliases this token may score, separated by spaces or commas; * means every endpoint of the project."
          >
            <Input id="tok-aliases" className="font-mono" placeholder="geo-mmm-prod" value={form.aliases} onChange={(e) => set("aliases", e.target.value)} />
          </Field>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Expires after" htmlFor="tok-exp" error={shown.expires}>
              <NativeSelect id="tok-exp" value={form.expires} onChange={(e) => set("expires", Number(e.target.value))}>
                {EXPIRY_CHOICES.map((d) => (
                  <option key={d} value={d}>
                    {d} days{d === DEFAULT_TOKEN_TTL_DAYS ? " (default)" : ""}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Rate limit (optional)" htmlFor="tok-rate" error={shown.rate} hint="Requests per minute.">
              <Input id="tok-rate" inputMode="numeric" placeholder="120" value={form.rate} onChange={(e) => set("rate", e.target.value)} />
            </Field>
            <div className="grid gap-1.5">
              <Field label="Compute budget (optional)" htmlFor="tok-budget" error={shown.budget} hint="vCPU-seconds; runs stop starting once spent.">
                <div className="flex gap-2">
                  <Input id="tok-budget" inputMode="decimal" placeholder="36000" value={form.budget} onChange={(e) => set("budget", e.target.value)} />
                  <NativeSelect aria-label="Budget period" className="w-28" value={form.period} onChange={(e) => set("period", e.target.value as BudgetPeriod)}>
                    <option value="day">per day</option>
                    <option value="month">per month</option>
                  </NativeSelect>
                </div>
              </Field>
            </div>
          </div>
          {create.isError ? <ErrorNotice error={create.error} title="The token could not be created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              <KeyRound /> Create token
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function SecretDialog({ created, onClose }: { created: TokenCreated; onClose: () => void }) {
  const [acknowledged, setAcknowledged] = useState(false);
  const { secret, token } = created;
  const close = () => {
    if (acknowledged || window.confirm("Close without copying? The token cannot be shown again; you would have to revoke it and create a new one.")) onClose();
  };
  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent wide onInteractOutside={(e) => e.preventDefault()}>
        <DialogTitle>Copy your new token now</DialogTitle>
        <DialogDescription>
          This is the only time the full token for <span className="font-medium text-foreground">{token.name}</span> is shown. Store it in a secret manager; the API keeps only a hash.
        </DialogDescription>
        <div className="grid gap-2">
          <Label htmlFor="tok-secret">Token</Label>
          <div className="flex items-center gap-1 rounded-md border bg-muted/60 p-1 pl-3">
            <input
              id="tok-secret"
              readOnly
              value={secret}
              onFocus={(e) => e.currentTarget.select()}
              className="min-w-0 flex-1 bg-transparent font-mono text-[13px] outline-none"
              spellCheck={false}
              autoComplete="off"
            />
            <CopyButton value={secret} label="Copy token" onCopied={() => setAcknowledged(true)} />
          </div>
          <p className="text-xs text-muted-foreground">
            Use it as <Mono>Authorization: Bearer mapi_…</Mono>. It works in {token.tenant_id}/{token.project_id} until {absoluteTime(token.expires_at)}.
          </p>
        </div>
        <details className="rounded-md border p-3 text-[13px]">
          <summary className="cursor-pointer font-medium">Use it from a shell</summary>
          <pre className="mt-2 overflow-x-auto rounded bg-muted/60 p-2 font-mono text-xs">{`export MAPI_TOKEN='<paste the token>'
curl -H "Authorization: Bearer $MAPI_TOKEN" ${env.apiBase}/v1/me`}</pre>
        </details>
        <label className="flex items-center gap-2 text-[13px]">
          <Checkbox checked={acknowledged} onCheckedChange={(v) => setAcknowledged(v === true)} />
          I have stored the token somewhere safe
        </label>
        <DialogFooter>
          <Button onClick={onClose} disabled={!acknowledged}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RevokeDialog({ token, onClose }: { token: ApiToken; onClose: () => void }) {
  const revoke = useRevokeToken();
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Revoke “{token.name}”?</DialogTitle>
        <DialogDescription>
          Programs using <Mono>{token.display}</Mono> get 401 Unauthenticated from their next request. This cannot be undone.
        </DialogDescription>
        {revoke.isError ? <ErrorNotice error={revoke.error} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            disabled={revoke.isPending}
            onClick={() =>
              revoke.mutate(token.id, {
                onSuccess: () => {
                  toast.success("Token revoked", token.name);
                  onClose();
                },
                onError: (e) => toast.error("Revoke failed", errorMessage(e)),
              })
            }
          >
            <Trash2 /> Revoke token
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
