/**
 * Governance (M3.5): policies for the project and the tenant with their versions, the check
 * catalog with About cards, the reviewers' queue, a validation's detail and a model version's
 * validations.
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { ClipboardCheck, Plus, ShieldCheck } from "lucide-react";
import { useMemo } from "react";

import { atLeast, can } from "@/api/access";
import { EmptyState, ErrorNotice, LoadingRows, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, NativeSelect } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { PersonName } from "@/features/auth/people";
import { useWorkspace } from "@/features/auth/workspace";
import { useGovernanceChecks, usePolicies, usePolicy, useReviewer, useTenantPolicies, useValidations } from "@/features/governance/api";
import { thresholdText } from "@/features/governance/evidence";
import { ModelValidations } from "@/features/governance/model-validations";
import { PolicyEditor, PolicyVersions } from "@/features/governance/policy-editor";
import { VALIDATION_META } from "@/features/governance/status";
import type { GovernancePolicy, ModelValidation, ValidationStatus } from "@/features/governance/types";
import { ValidationBadge, ValidationView } from "@/features/governance/validation-view";
import { relativeTime, shortId } from "@/lib/format";
import { VALIDATION_STATUSES } from "@/router";

const govRoute = getRouteApi("/governance");
const newPolicyRoute = getRouteApi("/governance/policies/new");
const policyRoute = getRouteApi("/governance/policies/$policyId");
const validationsRoute = getRouteApi("/validations");
const validationRoute = getRouteApi("/validations/$validationId");
const modelValidationsRoute = getRouteApi("/models/$modelId/versions/$version/validations");

function PolicyTable({ policies, editable }: { policies: GovernancePolicy[]; editable: (p: GovernancePolicy) => boolean }) {
  const navigate = useNavigate();
  const columns = useMemo<ColumnDef<GovernancePolicy, unknown>[]>(
    () => [
      {
        accessorKey: "name",
        header: "Policy",
        cell: ({ row }) => (
          <Link
            to="/governance/policies/$policyId"
            params={{ policyId: row.original.id }}
            search={row.original.scope === "tenant" ? { scope: "tenant" } : {}}
            className="font-medium hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            {row.original.name}
          </Link>
        ),
      },
      { accessorKey: "scope", header: "Scope", cell: ({ row }) => <Badge variant={row.original.scope === "tenant" ? "info" : "secondary"}>{row.original.scope === "tenant" ? "tenant-wide" : "project"}</Badge> },
      {
        id: "applies",
        header: "Covers",
        cell: ({ row }) => {
          const a = row.original.applies_to;
          const parts = [...(a?.families ?? []), ...(a?.models ?? []), ...(a?.templates ?? [])];
          return parts.length ? <Mono>{parts.join(", ")}</Mono> : <span className="text-muted-foreground">every model</span>;
        },
      },
      { id: "checks", header: "Checks", cell: ({ row }) => row.original.checks.length },
      {
        id: "flags",
        header: "Rules",
        cell: ({ row }) => (
          <span className="flex flex-wrap gap-1">
            {row.original.require_approval ? <Badge variant="warning">reviewer</Badge> : null}
            {row.original.require_validated_for_promotion ? <Badge variant="outline">gates promotion</Badge> : null}
            {row.original.active === false ? <Badge variant="outline">retired</Badge> : null}
          </span>
        ),
      },
      { accessorKey: "version", header: "Version", cell: ({ row }) => <Mono>v{row.original.version}</Mono> },
      { id: "edit", header: "", cell: ({ row }) => (editable(row.original) ? <span className="text-xs text-primary">Edit</span> : <span className="text-xs text-muted-foreground">View</span>) },
    ],
    [editable],
  );
  return (
    <DataTable
      label="Policies"
      data={policies}
      columns={columns}
      getRowId={(p) => p.id}
      onRowClick={(p) => void navigate({ to: "/governance/policies/$policyId", params: { policyId: p.id }, search: p.scope === "tenant" ? { scope: "tenant" } : {} })}
      empty="There are no policies."
    />
  );
}

function CheckCatalog() {
  const checks = useGovernanceChecks();
  return (
    <QueryView query={checks}>
      {(page) => (
        <div className="grid gap-4 lg:grid-cols-2" data-testid="check-catalog">
          {page.items.map((c) => (
            <AboutCard
              framed
              key={c.id}
              about={c.about}
              headingLevel={2}
              heading={
                <span className="flex flex-wrap items-center gap-2">
                  {c.title}
                  <Badge variant="outline" className="font-mono font-normal">
                    {c.id}
                  </Badge>
                </span>
              }
              actions={
                <span className="text-right text-xs text-muted-foreground">
                  {c.metric.replace(/_/g, " ")}
                  <br />
                  {thresholdText(c, c.warn)}
                </span>
              }
            />
          ))}
        </div>
      )}
    </QueryView>
  );
}

export function GovernancePage() {
  const search = govRoute.useSearch();
  const navigate = useNavigate({ from: "/governance" });
  const ws = useWorkspace();
  const reviewer = useReviewer();
  const tab = search.tab ?? "policies";
  const projectPolicies = usePolicies("all");
  const tenantPolicies = useTenantPolicies(tab === "tenant");
  const queue = useValidations("awaiting_approval");
  const projectAdmin = can(ws.grants, "create", "policy");
  const tenantAdmin = can(ws.grants, "create", "tenant_policy");
  const waiting = queue.data?.items.length ?? 0;
  return (
    <>
      <PageHeader
        title="Governance"
        description="Set the checks that a model must pass to become validated. These include null tests against random noise. Also set who approves the model and if endpoints can serve it."
        actions={
          <>
            <Button variant="ghost" asChild>
              <Link to="/validations">All validations</Link>
            </Button>
            {reviewer.isReviewer ? (
              <Button variant="outline" asChild>
                <Link to="/validations" search={{ status: "awaiting_approval" }}>
                  <ClipboardCheck /> Review queue {waiting ? <Badge variant="warning">{waiting}</Badge> : null}
                </Link>
              </Button>
            ) : null}
            {(tab === "tenant" ? tenantAdmin : projectAdmin) ? (
              <Button asChild>
                <Link to="/governance/policies/new" search={tab === "tenant" ? { scope: "tenant" } : {}}>
                  <Plus /> New {tab === "tenant" ? "tenant-wide " : ""}policy
                </Link>
              </Button>
            ) : null}
          </>
        }
      />
      <Tabs value={tab} onValueChange={(v) => void navigate({ search: { tab: v as typeof tab } })}>
        <TabsList>
          <TabsTrigger value="policies">In force here</TabsTrigger>
          <TabsTrigger value="tenant">Tenant-wide</TabsTrigger>
          <TabsTrigger value="checks">Check catalog</TabsTrigger>
        </TabsList>
        <TabsContent value="policies">
          <p className="mb-3 text-[13px] text-muted-foreground">All projects inherit the tenant-wide policies as a minimum. The policies of a project can add checks or make thresholds stricter.</p>
          <QueryView query={projectPolicies}>{(page) => <PolicyTable policies={page.items} editable={(p) => (p.scope === "tenant" ? tenantAdmin : projectAdmin)} />}</QueryView>
        </TabsContent>
        <TabsContent value="tenant">
          <QueryView query={tenantPolicies}>{(page) => <PolicyTable policies={page.items} editable={() => tenantAdmin} />}</QueryView>
        </TabsContent>
        <TabsContent value="checks">
          <CheckCatalog />
        </TabsContent>
      </Tabs>
    </>
  );
}

export function NewPolicyPage() {
  const { scope } = newPolicyRoute.useSearch();
  const ws = useWorkspace();
  const s = scope === "tenant" ? "tenant" : "project";
  const allowed = s === "tenant" ? can(ws.grants, "create", "tenant_policy") : can(ws.grants, "create", "policy");
  return (
    <>
      <PageHeader title={s === "tenant" ? "New tenant-wide policy" : "New project policy"} description="Select the checks and their limits. Then select who approves the model." />
      <PolicyEditor scope={s} policy={null} readOnly={!allowed} />
    </>
  );
}

export function PolicyPage() {
  const { policyId } = policyRoute.useParams();
  const { scope } = policyRoute.useSearch();
  const ws = useWorkspace();
  const s = scope === "tenant" ? "tenant" : "project";
  const policy = usePolicy(s, policyId);
  const allowed = s === "tenant" ? can(ws.grants, "update", "tenant_policy") : can(ws.grants, "update", "policy");
  if (policy.isPending) return <LoadingRows rows={6} />;
  if (policy.isError || !policy.data) return <ErrorNotice error={policy.error} title="The app could not load the policy" />;
  return (
    <>
      <PageHeader
        title={policy.data.name}
        description={policy.data.scope === "tenant" ? "A tenant-wide policy. All projects inherit it." : "A project policy."}
        meta={
          <>
            <span>version {policy.data.version}</span>
            <span>·</span>
            <span>saved {relativeTime(policy.data.created_at)}</span>
            {policy.data.active === false ? <Badge variant="outline">retired</Badge> : null}
          </>
        }
      />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <PolicyEditor key={`${policy.data.id}@${policy.data.version}`} scope={s} policy={policy.data} readOnly={!allowed || policy.data.active === false} />
        <PolicyVersions scope={s} policyId={policyId} />
      </div>
    </>
  );
}

// ----------------------------------------------------------------------------- validations

export function ValidationsPage() {
  const { status } = validationsRoute.useSearch();
  const navigate = useNavigate({ from: "/validations" });
  const reviewer = useReviewer();
  const ws = useWorkspace();
  const validations = useValidations(status);
  const columns = useMemo<ColumnDef<ModelValidation, unknown>[]>(
    () => [
      { accessorKey: "status", header: "Status", cell: ({ row }) => <ValidationBadge status={row.original.status} /> },
      {
        id: "model",
        header: "Model version",
        cell: ({ row }) => (
          <Link to="/validations/$validationId" params={{ validationId: row.original.id }} className="hover:underline" onClick={(e) => e.stopPropagation()}>
            <Mono>
              {shortId(row.original.model_id)} v{row.original.version}
            </Mono>
          </Link>
        ),
      },
      { id: "by", header: "Submitted by", cell: ({ row }) => (row.original.submitted_by === ws.user.id ? "you" : row.original.submitted_by ? <PersonName id={row.original.submitted_by} /> : "—") },
      { accessorKey: "submitted_at", header: "Submitted", cell: ({ row }) => (row.original.submitted_at ? relativeTime(row.original.submitted_at) : "—") },
      { id: "failing", header: "Blocking failures", cell: ({ row }) => (row.original.failing_checks?.length ? <Mono>{row.original.failing_checks.join(", ")}</Mono> : "—") },
    ],
    [ws.user.id],
  );
  const awaiting = status === "awaiting_approval";
  return (
    <>
      <PageHeader
        title={awaiting ? "Review queue" : "Validations"}
        description={
          awaiting
            ? reviewer.isReviewer
              ? "These model versions passed their checks and wait for a reviewer. You cannot approve your own submissions."
              : "These model versions wait for a reviewer. Only members with the reviewer capability and project admins can make the decision."
            : "All the validations of the model versions in this project."
        }
        actions={
          <NativeSelect
            aria-label="Filter by status"
            className="h-8 w-48"
            value={status ?? ""}
            onChange={(e) => void navigate({ search: { status: (e.target.value || undefined) as ValidationStatus | undefined } })}
          >
            <option value="">All statuses</option>
            {VALIDATION_STATUSES.map((s) => (
              <option key={s} value={s}>
                {VALIDATION_META[s].label}
              </option>
            ))}
          </NativeSelect>
        }
      />
      <QueryView query={validations}>
        {(page) =>
          page.items.length ? (
            <DataTable label="Validations" data={page.items} columns={columns} getRowId={(v) => v.id} onRowClick={(v) => void navigate({ to: "/validations/$validationId", params: { validationId: v.id } })} />
          ) : (
            <EmptyState icon={<ShieldCheck />} title={awaiting ? "Nothing to review" : "No validations"}>
              {awaiting ? "No model version waits for approval." : "To validate a model version, open the page of that version."}
            </EmptyState>
          )
        }
      </QueryView>
      {!reviewer.isReviewer && awaiting && atLeast(ws.current?.role, "viewer") ? <p className="mt-3 text-xs text-muted-foreground">Ask a project admin to give you the reviewer capability on the Members page.</p> : null}
    </>
  );
}

export function ValidationPage() {
  const { validationId } = validationRoute.useParams();
  return <ValidationView key={validationId} validationId={validationId} />;
}

export function ModelValidationsPage() {
  const { modelId, version } = modelValidationsRoute.useParams();
  const v = Number(version);
  if (!Number.isInteger(v) || v < 1) return <ErrorNotice error={new Error("The version must be a positive number.")} title="This is not a model version" />;
  return <ModelValidations modelId={modelId} version={v} />;
}
