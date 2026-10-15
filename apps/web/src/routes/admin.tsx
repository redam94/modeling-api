/**
 * Operator console (`/v1/admin/*`): tenants and trust tiers, quota policies, kill switches,
 * executor health and the audit log. Operators see metadata only, never tenant content, and
 * every change is audited server-side. Destructive switches need a typed confirmation.
 */
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import {
  Activity,
  Building2,
  CheckCircle2,
  CircleSlash,
  Gauge,
  ListChecks,
  Pencil,
  Plus,
  Power,
  ScrollText,
  ShieldCheck,
  ShieldOff,
  Trash2,
  XCircle,
} from "lucide-react";
import { Fragment, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { errorMessage } from "@/api/client";
import {
  useAdminCreateTenant,
  useAdminTenants,
  useAdminUpdateTenant,
  useAudit,
  useDeleteQuota,
  useExecutors,
  useKillSwitches,
  usePutExecutor,
  usePutKillSwitches,
  usePutQuota,
  useQuotaPolicies,
  type AuditFilters,
} from "@/api/queries";
import type {
  AuditAction,
  AuditEvent,
  AuditOutcome,
  ExecutorHealth,
  KillSwitchUpdate,
  QuotaLimits,
  QuotaPolicy,
  QuotaScope,
  ResourceClass,
  Tenant,
  TenantAdminUpdate,
  TenantStatus,
  TrustTier,
} from "@/api/types";
import { Meter } from "@/components/charts";
import { EmptyState, ErrorNotice, JsonBlock, LoadingRows, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { formatMicros, MICROS_PER_UNIT } from "@/lib/cost";
import { absoluteTime, formatBytes, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ADMIN_TABS, type AdminTab } from "@/router";
import { toast } from "@/stores/toast";

const route = getRouteApi("/admin");

const TIERS: TrustTier[] = ["internal", "verified", "public", "self-hosted-trusted"];
const STATUSES: TenantStatus[] = ["active", "suspended", "deleting"];
const RESIDENCIES = ["us", "eu", "asia"];

const TIER_META: Record<TrustTier, { label: string; description: string }> = {
  internal: { label: "Internal", description: "Our own teams. Process isolation is enough." },
  verified: { label: "Verified", description: "Known customers. gVisor or stronger, egress and DNS locked." },
  public: { label: "Public", description: "Self-serve sign-ups. Strongest isolation, small default quotas." },
  "self-hosted-trusted": { label: "Self-hosted", description: "A trusted single-tenant installation." },
};

const STATUS_VARIANT: Record<TenantStatus, "success" | "warning" | "danger"> = { active: "success", suspended: "warning", deleting: "danger" };

const TAB_META: Record<AdminTab, { label: string; icon: ReactNode }> = {
  tenants: { label: "Tenants", icon: <Building2 /> },
  quotas: { label: "Quotas", icon: <Gauge /> },
  "kill-switches": { label: "Kill switches", icon: <Power /> },
  executors: { label: "Executors", icon: <Activity /> },
  audit: { label: "Audit log", icon: <ScrollText /> },
  promotions: { label: "Promotions", icon: <ListChecks /> },
};

export function AdminPage() {
  const ws = useWorkspace();
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/admin" });
  const tab = search.tab ?? "tenants";
  if (!ws.operator) {
    return (
      <EmptyState icon={<ShieldOff />} title="The admin console is for platform operators">
        Your account is not a platform operator, so tenants, quotas and kill switches are not available to you. Organisation admins manage members and see usage from the
        Members and Usage pages.
      </EmptyState>
    );
  }
  return (
    <>
      <PageHeader
        title="Admin console"
        description="Platform operations. You see tenant metadata, usage and audit records, never tenant data; every change here is audited."
        meta={
          <Badge variant="info">
            <ShieldCheck /> Operator
          </Badge>
        }
      />
      <Tabs value={tab} onValueChange={(v) => void navigate({ search: { tab: v === "tenants" ? undefined : (v as AdminTab) } })}>
        <TabsList aria-label="Admin sections">
          {ADMIN_TABS.map((t) => (
            <TabsTrigger key={t} value={t}>
              {TAB_META[t].icon}
              {TAB_META[t].label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="tenants">
          <TenantsTab />
        </TabsContent>
        <TabsContent value="quotas">
          <QuotasTab />
        </TabsContent>
        <TabsContent value="kill-switches">
          <KillSwitchesTab />
        </TabsContent>
        <TabsContent value="executors">
          <ExecutorsTab />
        </TabsContent>
        <TabsContent value="audit">
          <AuditTab />
        </TabsContent>
        <TabsContent value="promotions">
          <EmptyState icon={<ListChecks />} title="No promotions waiting">
            Endpoint promotions gated on the sanity scorecard arrive with endpoints and aliases in v0.4. Requests to promote a model version to an endpoint will queue here for review.
          </EmptyState>
        </TabsContent>
      </Tabs>
    </>
  );
}

// ======================================================================== tenants

function TenantsTab() {
  const [status, setStatus] = useState<TenantStatus | "">("");
  const [tier, setTier] = useState<TrustTier | "">("");
  const tenants = useAdminTenants({ ...(status ? { status } : {}), ...(tier ? { trust_tier: tier } : {}) });
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Tenant | null>(null);
  const columns = useMemo<ColumnDef<Tenant, unknown>[]>(
    () => [
      {
        accessorKey: "name",
        header: "Tenant",
        cell: ({ row }) => (
          <div className="grid">
            <span className="font-medium">{row.original.name}</span>
            <Mono className="text-muted-foreground">{row.original.id}</Mono>
          </div>
        ),
      },
      {
        accessorKey: "trust_tier",
        header: "Tier",
        cell: ({ row }) => (
          <Badge variant={row.original.trust_tier === "public" ? "outline" : "secondary"} title={TIER_META[row.original.trust_tier].description}>
            {TIER_META[row.original.trust_tier].label}
          </Badge>
        ),
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ row }) => {
          const s = row.original.status ?? "active";
          return (
            <span className="grid">
              <Badge variant={STATUS_VARIANT[s]} className="w-fit">
                {s}
              </Badge>
              {row.original.status_reason ? <span className="max-w-64 truncate text-xs text-muted-foreground">{row.original.status_reason}</span> : null}
            </span>
          );
        },
      },
      { accessorKey: "data_residency", header: "Residency", cell: ({ row }) => <Mono>{row.original.data_residency ?? "us"}</Mono> },
      { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{relativeTime(row.original.created_at)}</span> },
      {
        id: "actions",
        header: () => <span className="sr-only">Actions</span>,
        enableSorting: false,
        cell: ({ row }) => (
          <Button variant="ghost" size="sm" onClick={() => setEditing(row.original)} aria-label={`Edit ${row.original.name}`}>
            <Pencil /> Edit
          </Button>
        ),
      },
    ],
    [],
  );
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <NativeSelect aria-label="Filter by status" className="h-8 w-36" value={status} onChange={(e) => setStatus(e.target.value as TenantStatus | "")}>
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect aria-label="Filter by tier" className="h-8 w-40" value={tier} onChange={(e) => setTier(e.target.value as TrustTier | "")}>
          <option value="">All tiers</option>
          {TIERS.map((t) => (
            <option key={t} value={t}>
              {TIER_META[t].label}
            </option>
          ))}
        </NativeSelect>
        <Button className="ml-auto" onClick={() => setCreating(true)}>
          <Plus /> New tenant
        </Button>
      </div>
      <QueryView query={tenants}>
        {(page) => <DataTable label="Tenants" data={page.items} columns={columns} getRowId={(t) => t.id} filterPlaceholder="Filter tenants" empty="No tenants match." />}
      </QueryView>
      {creating ? <CreateTenantDialog onClose={() => setCreating(false)} /> : null}
      {editing ? <EditTenantDialog tenant={editing} onClose={() => setEditing(null)} /> : null}
    </div>
  );
}

const SLUG_RE = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;
const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,255}$/;

function CreateTenantDialog({ onClose }: { onClose: () => void }) {
  const create = useAdminCreateTenant();
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [tier, setTier] = useState<TrustTier>("verified");
  const [residency, setResidency] = useState("us");
  const [admin, setAdmin] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const errors = {
    id: SLUG_RE.test(id) ? undefined : "Lowercase letters, digits, - and _ (start and end with a letter or digit).",
    name: name.trim() ? undefined : "Give the tenant a name.",
    admin: !admin.trim() || EMAIL_RE.test(admin.trim()) ? undefined : "Use an e-mail address.",
  };
  const valid = !errors.id && !errors.name && !errors.admin;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (!valid) return;
    create.mutate(
      { id, name: name.trim(), trust_tier: tier, data_residency: residency, ...(admin.trim() ? { initial_admin_email: admin.trim().toLowerCase() } : {}) },
      {
        onSuccess: (t) => {
          toast.success("Tenant created", `${t.name} (${t.id})`);
          onClose();
        },
      },
    );
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>New tenant</DialogTitle>
        <DialogDescription>A tenant is the billing, isolation and quota boundary. Its trust tier decides where its runs may be placed.</DialogDescription>
        <form className="grid gap-4" onSubmit={submit} noValidate>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Name" htmlFor="tn-name" error={submitted ? errors.name : undefined}>
              <Input id="tn-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </Field>
            <Field label="Id" htmlFor="tn-id" error={submitted ? errors.id : undefined} hint="Permanent; appears in storage paths.">
              <Input id="tn-id" className="font-mono" value={id} onChange={(e) => setId(e.target.value)} />
            </Field>
          </div>
          <Field label="Trust tier" htmlFor="tn-tier" hint={TIER_META[tier].description}>
            <NativeSelect id="tn-tier" value={tier} onChange={(e) => setTier(e.target.value as TrustTier)}>
              {TIERS.map((t) => (
                <option key={t} value={t}>
                  {TIER_META[t].label}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Data residency" htmlFor="tn-res">
              <NativeSelect id="tn-res" value={residency} onChange={(e) => setResidency(e.target.value)}>
                {RESIDENCIES.map((r) => (
                  <option key={r} value={r}>
                    {r.toUpperCase()}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="First admin (optional)" htmlFor="tn-admin" error={submitted ? errors.admin : undefined} hint="Invited by e-mail.">
              <Input id="tn-admin" type="email" value={admin} onChange={(e) => setAdmin(e.target.value)} />
            </Field>
          </div>
          {create.isError ? <ErrorNotice error={create.error} title="The tenant could not be created" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              Create tenant
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Only the fields that changed (the API rejects an empty update). */
export function tenantChanges(t: Tenant, next: { name: string; tier: TrustTier; status: TenantStatus; reason: string; residency: string }): TenantAdminUpdate {
  const out: TenantAdminUpdate = {};
  if (next.name.trim() && next.name.trim() !== t.name) out.name = next.name.trim();
  if (next.tier !== t.trust_tier) out.trust_tier = next.tier;
  if (next.status !== (t.status ?? "active")) out.status = next.status;
  if (next.residency !== (t.data_residency ?? "us")) out.data_residency = next.residency;
  if (out.status !== undefined && out.status !== "active") out.status_reason = next.reason.trim();
  else if (out.status === "active" && next.reason.trim()) out.status_reason = next.reason.trim();
  return out;
}

function EditTenantDialog({ tenant, onClose }: { tenant: Tenant; onClose: () => void }) {
  const update = useAdminUpdateTenant();
  const [name, setName] = useState(tenant.name);
  const [tier, setTier] = useState<TrustTier>(tenant.trust_tier);
  const [status, setStatus] = useState<TenantStatus>(tenant.status ?? "active");
  const [reason, setReason] = useState("");
  const [residency, setResidency] = useState(tenant.data_residency ?? "us");
  const [confirm, setConfirm] = useState("");
  const changes = tenantChanges(tenant, { name, tier, status, reason, residency });
  const needsReason = changes.status !== undefined && changes.status !== "active";
  const dangerous = changes.status === "deleting" || changes.status === "suspended";
  const blocked = Object.keys(changes).length === 0 || (needsReason && !reason.trim()) || (dangerous && confirm !== tenant.id);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Edit {tenant.name}</DialogTitle>
        <DialogDescription>
          <Mono>{tenant.id}</Mono> · created {relativeTime(tenant.created_at)}
        </DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (blocked) return;
            update.mutate(
              { tenantId: tenant.id, body: changes },
              {
                onSuccess: () => {
                  toast.success("Tenant updated", tenant.name);
                  onClose();
                },
              },
            );
          }}
        >
          <Field label="Name" htmlFor="te-name">
            <Input id="te-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
          </Field>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Trust tier" htmlFor="te-tier">
              <NativeSelect id="te-tier" value={tier} onChange={(e) => setTier(e.target.value as TrustTier)}>
                {TIERS.map((t) => (
                  <option key={t} value={t}>
                    {TIER_META[t].label}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Status" htmlFor="te-status">
              <NativeSelect id="te-status" value={status} onChange={(e) => setStatus(e.target.value as TenantStatus)}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Residency" htmlFor="te-res" hint="New runs only.">
              <NativeSelect id="te-res" value={residency} onChange={(e) => setResidency(e.target.value)}>
                {RESIDENCIES.map((r) => (
                  <option key={r} value={r}>
                    {r.toUpperCase()}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>
          {changes.trust_tier ? <p className="text-xs text-muted-foreground">{TIER_META[tier].description} Running work keeps its placement.</p> : null}
          {needsReason || status !== (tenant.status ?? "active") ? (
            <Field label={needsReason ? "Reason (required, shown to the tenant)" : "Reason (optional)"} htmlFor="te-reason">
              <Textarea id="te-reason" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            </Field>
          ) : null}
          {dangerous ? (
            <div className="grid gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-3">
              <p className="text-[13px]">
                {changes.status === "deleting"
                  ? "Deleting stops all access to this tenant and schedules its data for removal."
                  : "Suspending makes the tenant read-only: no runs, uploads or changes."}{" "}
                Type <Mono>{tenant.id}</Mono> to confirm.
              </p>
              <Input aria-label="Type the tenant id to confirm" className="font-mono" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </div>
          ) : null}
          {update.isError ? <ErrorNotice error={update.error} title="The tenant could not be updated" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant={dangerous ? "destructive" : "default"} disabled={blocked || update.isPending}>
              Save changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ======================================================================== quotas

const LIMIT_FIELDS: { key: keyof QuotaLimits; label: string; unit: string; integer: boolean }[] = [
  { key: "vcpu_seconds_per_day", label: "Compute per day", unit: "vCPU·s", integer: false },
  { key: "vcpu_seconds_per_month", label: "Compute per month", unit: "vCPU·s", integer: false },
  { key: "concurrent_runs", label: "Concurrent runs", unit: "runs", integer: true },
  { key: "max_fanout_tasks", label: "Tasks per run", unit: "tasks", integer: true },
  { key: "storage_bytes", label: "Storage", unit: "bytes", integer: true },
  { key: "egress_bytes_per_month", label: "Downloads per month", unit: "bytes", integer: true },
  { key: "requests_per_minute", label: "Requests per minute", unit: "req/min", integer: true },
];

const CLASSES: ResourceClass[] = ["S", "M", "L", "XL"];
const SCOPE_ORDER: Record<QuotaScope, number> = { global: 0, tenant: 1, token: 2 };

function limitText(key: keyof QuotaLimits, v: QuotaLimits[keyof QuotaLimits]): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  const n = v;
  if (key === "storage_bytes" || key === "egress_bytes_per_month") return formatBytes(n);
  return n.toLocaleString("en");
}

function QuotasTab() {
  const [scope, setScope] = useState<QuotaScope | "">("");
  const policies = useQuotaPolicies(scope ? { scope } : {});
  const [editing, setEditing] = useState<QuotaPolicy | "new" | null>(null);
  const [deleting, setDeleting] = useState<QuotaPolicy | null>(null);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <NativeSelect aria-label="Filter by scope" className="h-8 w-40" value={scope} onChange={(e) => setScope(e.target.value as QuotaScope | "")}>
          <option value="">All scopes</option>
          <option value="global">Global</option>
          <option value="tenant">Tenant</option>
          <option value="token">Token</option>
        </NativeSelect>
        <p className="text-xs text-muted-foreground">Checked token → tenant → global; the first limit hit wins. Without a policy the tier default applies.</p>
        <Button className="ml-auto" onClick={() => setEditing("new")}>
          <Plus /> New policy
        </Button>
      </div>
      <QueryView query={policies}>
        {(page) =>
          page.items.length === 0 ? (
            <EmptyState icon={<Gauge />} title="No quota policies">
              Tier defaults apply everywhere.
            </EmptyState>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
              {[...page.items]
                .sort((a, b) => SCOPE_ORDER[a.scope] - SCOPE_ORDER[b.scope] || a.subject_id.localeCompare(b.subject_id))
                .map((p) => (
                  <Card key={`${p.scope}:${p.subject_id}`} aria-label={`${p.scope} ${p.subject_id}`}>
                    <CardHeader>
                      <div className="min-w-0">
                        <CardTitle className="flex items-center gap-2">
                          <Badge variant={p.scope === "global" ? "default" : "secondary"}>{p.scope}</Badge>
                          <Mono className="truncate">{p.subject_id}</Mono>
                        </CardTitle>
                        {p.note ? <CardDescription className="truncate">{p.note}</CardDescription> : null}
                      </div>
                      <div className="flex gap-1">
                        <Button variant="ghost" size="icon-sm" aria-label={`Edit ${p.scope} ${p.subject_id}`} onClick={() => setEditing(p)}>
                          <Pencil />
                        </Button>
                        <Button variant="ghost" size="icon-sm" aria-label={`Delete ${p.scope} ${p.subject_id}`} onClick={() => setDeleting(p)}>
                          <Trash2 />
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-[13px]">
                        {[...LIMIT_FIELDS.map((f) => f.key), "max_resource_class" as const]
                          .filter((k) => p.limits[k] != null)
                          .map((k) => (
                            <Fragment key={k}>
                              <dt className="text-muted-foreground">{LIMIT_FIELDS.find((f) => f.key === k)?.label ?? "Largest class"}</dt>
                              <dd className="text-right tabular">{limitText(k, p.limits[k])}</dd>
                            </Fragment>
                          ))}
                      </dl>
                      {p.updated_at ? <p className="mt-3 text-xs text-muted-foreground">Updated {relativeTime(p.updated_at)}{p.updated_by ? ` by ${p.updated_by}` : ""}</p> : null}
                    </CardContent>
                  </Card>
                ))}
            </div>
          )
        }
      </QueryView>
      {editing ? <QuotaDialog policy={editing === "new" ? null : editing} onClose={() => setEditing(null)} /> : null}
      {deleting ? <DeleteQuotaDialog policy={deleting} onClose={() => setDeleting(null)} /> : null}
    </div>
  );
}

/** Parse the limit inputs: blank means "no limit at this level". */
export function parseLimits(values: Record<string, string>, maxClass: string): { limits: QuotaLimits; errors: Record<string, string> } {
  const limits: QuotaLimits = {};
  const errors: Record<string, string> = {};
  for (const f of LIMIT_FIELDS) {
    const raw = (values[f.key] ?? "").trim().replace(/[_,\s]/g, "");
    if (!raw) continue;
    const n = Number(raw);
    const min = f.key === "max_fanout_tasks" || f.key === "requests_per_minute" ? 1 : 0;
    if (!Number.isFinite(n) || n < min || (f.integer && !Number.isInteger(n)) || (f.key === "max_fanout_tasks" && n > 10_000)) {
      errors[f.key] = f.integer ? `A whole number of at least ${min}.` : "A number of at least 0.";
      continue;
    }
    (limits as Record<string, number>)[f.key] = n;
  }
  if (maxClass) limits.max_resource_class = maxClass as ResourceClass;
  return { limits, errors };
}

function QuotaDialog({ policy, onClose }: { policy: QuotaPolicy | null; onClose: () => void }) {
  const put = usePutQuota();
  const [scope, setScope] = useState<QuotaScope>(policy?.scope ?? "tenant");
  const [subject, setSubject] = useState(policy?.subject_id ?? "");
  const [note, setNote] = useState(policy?.note ?? "");
  const [maxClass, setMaxClass] = useState<string>(policy?.limits.max_resource_class ?? "");
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(LIMIT_FIELDS.map((f) => [f.key, policy?.limits[f.key] != null ? String(policy.limits[f.key]) : ""])),
  );
  const { limits, errors } = parseLimits(values, maxClass);
  const subjectId = scope === "global" ? "*" : subject.trim();
  const subjectError = scope === "global" ? undefined : scope === "token" ? (/^tok_[0-9a-z]{26}$/.test(subjectId) ? undefined : "A token id (tok_…).") : SLUG_RE.test(subjectId) ? undefined : "A tenant id.";
  const blocked = Object.keys(errors).length > 0 || Boolean(subjectError);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle>{policy ? "Edit quota policy" : "New quota policy"}</DialogTitle>
        <DialogDescription>Saving replaces the whole policy for this scope and subject. Leave a field blank for no limit at this level.</DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (blocked) return;
            put.mutate(
              { scope, subject_id: subjectId, limits, ...(note.trim() ? { note: note.trim() } : {}) },
              {
                onSuccess: () => {
                  toast.success("Quota policy saved", `${scope} ${subjectId}`);
                  onClose();
                },
              },
            );
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Scope" htmlFor="qp-scope">
              <NativeSelect id="qp-scope" value={scope} disabled={Boolean(policy)} onChange={(e) => setScope(e.target.value as QuotaScope)}>
                <option value="global">Global (whole platform)</option>
                <option value="tenant">Tenant</option>
                <option value="token">Token</option>
              </NativeSelect>
            </Field>
            <Field label="Subject" htmlFor="qp-subject" error={subject || policy ? subjectError : undefined}>
              <Input
                id="qp-subject"
                className="font-mono"
                disabled={Boolean(policy) || scope === "global"}
                value={scope === "global" ? "*" : subject}
                placeholder={scope === "token" ? "tok_…" : "acme"}
                onChange={(e) => setSubject(e.target.value)}
              />
            </Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {LIMIT_FIELDS.map((f) => (
              <Field key={f.key} label={f.label} htmlFor={`qp-${f.key}`} error={errors[f.key]} hint={f.unit}>
                <Input id={`qp-${f.key}`} inputMode="decimal" value={values[f.key] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))} />
              </Field>
            ))}
            <Field label="Largest class" htmlFor="qp-class" hint="Runs above are refused.">
              <NativeSelect id="qp-class" value={maxClass} onChange={(e) => setMaxClass(e.target.value)}>
                <option value="">No limit</option>
                {CLASSES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>
          <Field label="Note (optional)" htmlFor="qp-note">
            <Input id="qp-note" maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why this policy exists" />
          </Field>
          {put.isError ? <ErrorNotice error={put.error} title="The policy could not be saved" /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={blocked || put.isPending}>
              Save policy
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeleteQuotaDialog({ policy, onClose }: { policy: QuotaPolicy; onClose: () => void }) {
  const del = useDeleteQuota();
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>
          Delete the {policy.scope} policy for <Mono>{policy.subject_id}</Mono>?
        </DialogTitle>
        <DialogDescription>The tier default applies again from the next request.</DialogDescription>
        {del.isError ? <ErrorNotice error={del.error} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={del.isPending}
            onClick={() =>
              del.mutate(
                { scope: policy.scope, subjectId: policy.subject_id },
                {
                  onSuccess: () => {
                    toast.success("Quota policy deleted");
                    onClose();
                  },
                },
              )
            }
          >
            <Trash2 /> Delete policy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ======================================================================== kill switches

type PendingSwitch = { title: string; description: string; body: Omit<KillSwitchUpdate, "reason">; danger: boolean; confirmWord?: string };

function KillSwitchesTab() {
  const switches = useKillSwitches();
  const [pending, setPending] = useState<PendingSwitch | null>(null);
  const [cap, setCap] = useState("");
  const [period, setPeriod] = useState<"day" | "month">("day");
  return (
    <QueryView query={switches}>
      {(ks) => {
        const enabled = ks.untrusted_execution_enabled ?? true;
        const capMicros = ks.global_spend_cap_micros ?? null;
        const currency = ks.spend_cap_currency ?? "USD";
        const capValue = Number(cap);
        const capValid = cap.trim() !== "" && Number.isFinite(capValue) && capValue >= 0;
        return (
          <div className="grid gap-5">
            {ks.updated_at ? (
              <p className="text-xs text-muted-foreground">
                Last changed {relativeTime(ks.updated_at)}
                {ks.updated_by ? ` by ${ks.updated_by}` : ""}
                {ks.reason ? (
                  <>
                    : <span className="text-foreground">{ks.reason}</span>
                  </>
                ) : null}
              </p>
            ) : null}
            <div className="grid items-start gap-5 lg:grid-cols-2">
              <Card className={cn(!enabled && "border-destructive/40")}>
                <CardHeader>
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      Uploaded code
                      <Badge variant={enabled ? "success" : "danger"}>{enabled ? "running" : "paused"}</Badge>
                    </CardTitle>
                    <CardDescription>Runs of tenant-uploaded plugin code (T2/T3). Built-in models are never affected.</CardDescription>
                  </div>
                  <Switch
                    checked={enabled}
                    aria-label="Allow uploaded code to run"
                    onCheckedChange={(on) =>
                      setPending(
                        on
                          ? { title: "Resume uploaded code?", description: "Queued runs of uploaded plugins become eligible again.", body: { untrusted_execution_enabled: true }, danger: false }
                          : {
                              title: "Pause all uploaded code?",
                              description: "No new run of tenant-uploaded plugin code is accepted or leased, on every tenant. Runs already executing continue: use “Cancel running uploaded code” to stop them too.",
                              body: { untrusted_execution_enabled: false },
                              danger: true,
                              confirmWord: "PAUSE",
                            },
                      )
                    }
                  />
                </CardHeader>
                <CardContent>
                  <Button
                    variant="outline"
                    className="text-destructive"
                    onClick={() =>
                      setPending({
                        title: "Cancel all running uploaded code?",
                        description:
                          "Pauses uploaded code and cancels every unfinished run of it on every tenant, including runs already executing. Use it to contain a suspected sandbox escape.",
                        body: { untrusted_execution_enabled: false, cancel_untrusted_runs: true },
                        danger: true,
                        confirmWord: "CANCEL",
                      })
                    }
                  >
                    <ShieldOff /> Cancel running uploaded code
                  </Button>
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      Global spend cap
                      <Badge variant={capMicros == null ? "outline" : "info"}>{capMicros == null ? "none" : `${formatMicros(capMicros, currency)} per ${ks.spend_cap_period ?? "day"}`}</Badge>
                    </CardTitle>
                    <CardDescription>When platform spend in the window reaches the cap, non-internal tenants cannot start runs until it resets.</CardDescription>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-3">
                  <div className="flex flex-wrap items-end gap-2">
                    <Field label={`New cap (${currency})`} htmlFor="ks-cap" className="w-40">
                      <Input id="ks-cap" inputMode="decimal" placeholder="500" value={cap} onChange={(e) => setCap(e.target.value)} />
                    </Field>
                    <NativeSelect aria-label="Cap window" className="w-32" value={period} onChange={(e) => setPeriod(e.target.value as "day" | "month")}>
                      <option value="day">per day</option>
                      <option value="month">per month</option>
                    </NativeSelect>
                    <Button
                      variant="outline"
                      disabled={!capValid}
                      onClick={() =>
                        setPending({
                          title: `Set the spend cap to ${formatMicros(Math.round(capValue * MICROS_PER_UNIT), currency)} per ${period}?`,
                          description: "Applies from the next run submission.",
                          body: { global_spend_cap_micros: Math.round(capValue * MICROS_PER_UNIT), spend_cap_period: period },
                          danger: false,
                        })
                      }
                    >
                      Set cap
                    </Button>
                    {capMicros != null ? (
                      <Button
                        variant="ghost"
                        onClick={() => setPending({ title: "Remove the spend cap?", description: "Spend is then limited only by per-tenant quotas.", body: { clear_spend_cap: true }, danger: true, confirmWord: "REMOVE" })}
                      >
                        <CircleSlash /> Remove cap
                      </Button>
                    ) : null}
                  </div>
                </CardContent>
              </Card>
            </div>
            {pending ? <KillSwitchConfirm pending={pending} onClose={() => setPending(null)} /> : null}
          </div>
        );
      }}
    </QueryView>
  );
}

function KillSwitchConfirm({ pending, onClose }: { pending: PendingSwitch; onClose: () => void }) {
  const put = usePutKillSwitches();
  const [reason, setReason] = useState("");
  const [word, setWord] = useState("");
  const blocked = reason.trim().length < 3 || (pending.confirmWord !== undefined && word !== pending.confirmWord);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>{pending.title}</DialogTitle>
        <DialogDescription>{pending.description}</DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (blocked) return;
            put.mutate(
              { ...pending.body, reason: reason.trim() },
              {
                onSuccess: () => {
                  toast.success("Kill switches updated");
                  onClose();
                },
                onError: (err) => toast.error("The change was not applied", errorMessage(err)),
              },
            );
          }}
        >
          <Field label="Reason (recorded in the audit log)" htmlFor="ks-reason" hint="At least 3 characters, e.g. an incident id.">
            <Textarea id="ks-reason" autoFocus maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          {pending.confirmWord ? (
            <Field label={`Type ${pending.confirmWord} to confirm`} htmlFor="ks-word">
              <Input id="ks-word" className="font-mono" autoComplete="off" value={word} onChange={(e) => setWord(e.target.value)} />
            </Field>
          ) : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant={pending.danger ? "destructive" : "default"} disabled={blocked || put.isPending}>
              Confirm
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ======================================================================== executors

const ISOLATION = ["none", "process", "gVisor", "microVM", "dedicated VM"];
const EXEC_STATUS: Record<ExecutorHealth["status"], { variant: "success" | "warning" | "danger" | "outline"; dot: string }> = {
  ok: { variant: "success", dot: "bg-success" },
  degraded: { variant: "warning", dot: "bg-warning" },
  down: { variant: "danger", dot: "bg-destructive" },
  disabled: { variant: "outline", dot: "bg-muted-foreground/50" },
};

function ExecutorCard({ e }: { e: ExecutorHealth }) {
  const st = EXEC_STATUS[e.status];
  const [toggling, setToggling] = useState(false);
  const rate = e.submit_rate_limit_per_minute ? (e.submits_last_minute ?? 0) / e.submit_rate_limit_per_minute : null;
  const failed = (e.conformance?.results ?? []).filter((r) => r.outcome === "failed" || r.outcome === "error");
  return (
    <Card aria-label={`Executor ${e.name}`}>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-2">
            <span aria-hidden className={cn("size-2 rounded-full", st.dot)} />
            <Mono className="text-[14px]">{e.name}</Mono>
            <Badge variant={st.variant}>{e.status}</Badge>
          </CardTitle>
          <CardDescription>
            {ISOLATION[e.caps.isolation] ?? e.caps.isolation} isolation · classes {(e.caps.classes ?? []).join(", ") || "—"}
            {(e.regions ?? e.caps.regions ?? []).length ? ` · ${(e.regions ?? e.caps.regions ?? []).join(", ")}` : ""}
          </CardDescription>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Badge variant={e.enabled ? "secondary" : "outline"}>{e.enabled ? "enabled" : "disabled"}</Badge>
          <Button size="sm" variant={e.enabled ? "outline" : "default"} onClick={() => setToggling(true)}>
            <Power /> {e.enabled ? "Disable" : "Enable"}
          </Button>
        </div>
        {toggling ? <ExecutorToggleConfirm e={e} onClose={() => setToggling(false)} /> : null}
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="flex flex-wrap gap-1.5">
          <Badge variant={e.conformance_passed ? "success" : "danger"}>
            {e.conformance_passed ? <CheckCircle2 /> : <XCircle />} conformance {e.conformance_passed ? "passed" : "not passed"}
          </Badge>
          <Badge variant={e.allows_public_tenants ? "info" : "outline"}>{e.allows_public_tenants ? "public tenants allowed" : "trusted tenants only"}</Badge>
          {e.caps.egress_enforced ? <Badge variant="outline">egress locked</Badge> : null}
          {e.caps.dns_enforced ? <Badge variant="outline">DNS locked</Badge> : null}
        </div>
        <dl className="grid grid-cols-3 gap-2 text-center">
          {[
            ["Running", e.running_executions ?? 0],
            ["Queued", e.queued_runs ?? 0],
            ["Failures 1 h", e.failures_last_hour ?? 0],
          ].map(([k, v]) => (
            <div key={String(k)} className="rounded-md bg-muted/60 p-2">
              <dt className="text-[11px] text-muted-foreground">{k}</dt>
              <dd className="text-lg font-semibold tabular">{Number(v).toLocaleString("en")}</dd>
            </div>
          ))}
        </dl>
        {rate !== null ? (
          <div className="grid gap-1">
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>Submit rate</span>
              <span className="tabular">
                {e.submits_last_minute ?? 0} / {e.submit_rate_limit_per_minute} per min{e.throttled_last_hour ? ` · ${e.throttled_last_hour} throttled in 1 h` : ""}
              </span>
            </div>
            <Meter label={`Submit rate of ${e.name}`} fraction={rate} valueText={`${e.submits_last_minute ?? 0} of ${e.submit_rate_limit_per_minute} per minute`} />
          </div>
        ) : null}
        {failed.length ? (
          <details>
            <summary className="cursor-pointer text-xs text-destructive">{failed.length} conformance case(s) failing</summary>
            <ul className="mt-1 grid gap-1 text-xs">
              {failed.map((r) => (
                <li key={r.case_id}>
                  <Mono>{r.case_id}</Mono> {r.problems?.length ? <span className="text-muted-foreground">— {r.problems.join("; ")}</span> : null}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
        <p className="text-xs text-muted-foreground">
          {e.message ? `${e.message} · ` : ""}
          Last success {e.last_success_at ? relativeTime(e.last_success_at) : "never"} · checked {relativeTime(e.checked_at)}
        </p>
      </CardContent>
    </Card>
  );
}

function ExecutorToggleConfirm({ e, onClose }: { e: ExecutorHealth; onClose: () => void }) {
  const put = usePutExecutor();
  const [reason, setReason] = useState("");
  const [word, setWord] = useState("");
  const disabling = e.enabled;
  const blocked = reason.trim().length < 3 || (disabling && word !== "DISABLE");
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>{disabling ? `Disable ${e.name}?` : `Enable ${e.name}?`}</DialogTitle>
        <DialogDescription>
          {disabling
            ? "No new run is placed on this executor. Runs already on it keep running: cancel them from the kill switches if needed."
            : "The dispatcher places runs on this executor again."}
        </DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(ev) => {
            ev.preventDefault();
            if (blocked) return;
            put.mutate(
              { name: e.name, body: { enabled: !disabling, reason: reason.trim() } },
              {
                onSuccess: () => {
                  toast.success(disabling ? `${e.name} disabled` : `${e.name} enabled`);
                  onClose();
                },
                onError: (err) => toast.error("The change was not applied", errorMessage(err)),
              },
            );
          }}
        >
          <Field label="Reason (recorded in the audit log)" htmlFor="ex-reason" hint="At least 3 characters, e.g. an incident id.">
            <Textarea id="ex-reason" autoFocus maxLength={500} value={reason} onChange={(ev) => setReason(ev.target.value)} />
          </Field>
          {disabling ? (
            <Field label="Type DISABLE to confirm" htmlFor="ex-word">
              <Input id="ex-word" className="font-mono" autoComplete="off" value={word} onChange={(ev) => setWord(ev.target.value)} />
            </Field>
          ) : null}
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant={disabling ? "destructive" : "default"} disabled={blocked || put.isPending}>
              Confirm
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ExecutorsTab() {
  const executors = useExecutors();
  return (
    <QueryView query={executors}>
      {(page) =>
        page.items.length === 0 ? (
          <EmptyState icon={<Activity />} title="No executors registered" />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
            {page.items.map((e) => (
              <ExecutorCard key={e.name} e={e} />
            ))}
          </div>
        )
      }
    </QueryView>
  );
}

// ======================================================================== audit

const AUDIT_ACTIONS: AuditAction[] = [
  "auth.login",
  "auth.login_failed",
  "auth.logout",
  "tenant.create",
  "tenant.update",
  "tenant.suspend",
  "tenant.reactivate",
  "tenant.delete",
  "project.create",
  "project.update",
  "project.delete",
  "member.add",
  "member.update",
  "member.remove",
  "token.create",
  "token.revoke",
  "token.auth_failed",
  "quota.update",
  "quota.delete",
  "quota.denied",
  "kill_switch.update",
  "executor.update",
  "run.create",
  "run.cancel",
  "run.integrity_failure",
  "dataset.download",
  "model.artifact_link",
  "endpoint.promote",
  "plugin.publish",
  "plugin.yank",
  "event.rejected",
];

const OUTCOME_VARIANT: Record<AuditOutcome, "success" | "warning" | "danger"> = { success: "success", denied: "warning", error: "danger" };

/** `YYYY-MM-DD` from a date input → an RFC 3339 instant (start or end of that UTC day). */
export function dayBound(value: string, end: boolean): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined;
  return end ? `${value}T23:59:59Z` : `${value}T00:00:00Z`;
}

function AuditTab() {
  const [draft, setDraft] = useState({ tenant_id: "", actor_id: "", action: "", outcome: "", since: "", until: "" });
  const [filters, setFilters] = useState<AuditFilters>({});
  const audit = useAudit(filters, null);
  const [open, setOpen] = useState<string | null>(null);
  const rows = audit.data?.pages.flatMap((p) => p.items) ?? [];
  const apply = (e: FormEvent) => {
    e.preventDefault();
    setFilters({
      ...(draft.tenant_id.trim() ? { tenant_id: draft.tenant_id.trim() } : {}),
      ...(draft.actor_id.trim() ? { actor_id: draft.actor_id.trim() } : {}),
      ...(draft.action ? { action: draft.action as AuditAction } : {}),
      ...(draft.outcome ? { outcome: draft.outcome as AuditOutcome } : {}),
      ...(dayBound(draft.since, false) ? { since: dayBound(draft.since, false) } : {}),
      ...(dayBound(draft.until, true) ? { until: dayBound(draft.until, true) } : {}),
    });
  };
  const set = (k: keyof typeof draft) => (e: { target: { value: string } }) => setDraft((d) => ({ ...d, [k]: e.target.value }));
  return (
    <div className="grid gap-4">
      <form onSubmit={apply} className="grid gap-3 rounded-lg border bg-card p-4 sm:grid-cols-3 xl:grid-cols-7" aria-label="Audit filters">
        <Field label="Tenant" htmlFor="au-tenant">
          <Input id="au-tenant" className="font-mono" value={draft.tenant_id} onChange={set("tenant_id")} placeholder="acme" />
        </Field>
        <Field label="Actor" htmlFor="au-actor">
          <Input id="au-actor" className="font-mono" value={draft.actor_id} onChange={set("actor_id")} placeholder="usr_…" />
        </Field>
        <Field label="Action" htmlFor="au-action">
          <NativeSelect id="au-action" value={draft.action} onChange={set("action")}>
            <option value="">Any</option>
            {AUDIT_ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field label="Outcome" htmlFor="au-outcome">
          <NativeSelect id="au-outcome" value={draft.outcome} onChange={set("outcome")}>
            <option value="">Any</option>
            <option value="success">success</option>
            <option value="denied">denied</option>
            <option value="error">error</option>
          </NativeSelect>
        </Field>
        <Field label="From (UTC)" htmlFor="au-since">
          <Input id="au-since" type="date" value={draft.since} onChange={set("since")} />
        </Field>
        <Field label="To (UTC)" htmlFor="au-until">
          <Input id="au-until" type="date" value={draft.until} onChange={set("until")} />
        </Field>
        <div className="flex items-end gap-2">
          <Button type="submit">Apply</Button>
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setDraft({ tenant_id: "", actor_id: "", action: "", outcome: "", since: "", until: "" });
              setFilters({});
            }}
          >
            Reset
          </Button>
        </div>
      </form>
      {audit.isPending ? (
        <LoadingRows rows={6} />
      ) : audit.isError ? (
        <ErrorNotice error={audit.error} title="The audit log could not be loaded" />
      ) : rows.length === 0 ? (
        <EmptyState icon={<ScrollText />} title="No audit events match" />
      ) : (
        <div className="max-h-[70vh] overflow-auto rounded-lg border bg-card [contain:inline-size]">
          <table className="w-full text-[13px]" aria-label="Audit log">
            <thead className="sticky top-0 z-[1] bg-card">
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="h-9 px-3 font-medium">Time (UTC)</th>
                <th scope="col" className="px-3 font-medium">Action</th>
                <th scope="col" className="px-3 font-medium">Outcome</th>
                <th scope="col" className="px-3 font-medium">Actor</th>
                <th scope="col" className="px-3 font-medium">Tenant / project</th>
                <th scope="col" className="px-3 font-medium">Resource</th>
                <th scope="col" className="px-3">
                  <span className="sr-only">Details</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <AuditRow key={e.id} e={e} open={open === e.id} onToggle={() => setOpen(open === e.id ? null : e.id)} />
              ))}
            </tbody>
          </table>
          {audit.hasNextPage ? (
            <div className="border-t p-2 text-center">
              <Button variant="ghost" size="sm" disabled={audit.isFetchingNextPage} onClick={() => void audit.fetchNextPage()}>
                Load older events
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function AuditRow({ e, open, onToggle }: { e: AuditEvent; open: boolean; onToggle: () => void }) {
  return (
    <>
      <tr className="border-b last:border-0">
        <td className="px-3 py-2 whitespace-nowrap tabular" title={e.ts}>
          {absoluteTime(e.ts)}
        </td>
        <td className="px-3 py-2">
          <Mono>{e.action}</Mono>
          {e.operator ? (
            <Badge variant="info" className="ml-1.5">
              operator
            </Badge>
          ) : null}
        </td>
        <td className="px-3 py-2">
          <Badge variant={OUTCOME_VARIANT[e.outcome]}>{e.outcome}</Badge>
          {e.code ? <Mono className="ml-1.5 text-muted-foreground">{e.code}</Mono> : null}
        </td>
        <td className="px-3 py-2">
          <Mono className="text-muted-foreground">{e.actor_id ?? e.actor_kind ?? "—"}</Mono>
        </td>
        <td className="px-3 py-2">
          <Mono>{[e.tenant_id, e.project_id].filter(Boolean).join("/") || "—"}</Mono>
        </td>
        <td className="px-3 py-2">
          <span className="text-muted-foreground">{e.resource_kind}</span> {e.resource_id ? <Mono>{e.resource_id}</Mono> : null}
        </td>
        <td className="px-3 py-2 text-right">
          <Button variant="ghost" size="sm" aria-expanded={open} aria-label={`Details of ${e.action} at ${absoluteTime(e.ts)}`} onClick={onToggle}>
            {open ? "Hide" : "Details"}
          </Button>
        </td>
      </tr>
      {open ? (
        <tr className="border-b bg-muted/30">
          <td colSpan={7} className="px-3 py-3">
            <JsonBlock
              maxHeight="16rem"
              value={{ id: e.id, request_id: e.request_id, token_id: e.token_id, ip_hash: e.ip_hash, user_agent_hash: e.user_agent_hash, details: e.details ?? {} }}
            />
          </td>
        </tr>
      ) : null}
    </>
  );
}
