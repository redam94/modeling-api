import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, createRoute, createRouter, lazyRouteComponent, Link } from "@tanstack/react-router";

import type { RunKind, RunState } from "@/api/types";
import { EmptyState } from "@/components/common";
import { AppShell } from "@/components/shell/app-shell";
import { RUN_KINDS, RUN_STATES } from "@/lib/run-state";
import { HomePage } from "@/routes/home";

/*
 * Every page except Home is a lazy route: its module (and what only it needs, such as Monaco
 * on the spec editor or Apache Arrow on Score and Inspect) loads on navigation or on link
 * intent, never on first paint. `bundle-budget.ts` fails the build if that regresses.
 */
const page = {
  admin: () => import("@/routes/admin"),
  build: () => import("@/routes/build"),
  catalog: () => import("@/routes/catalog"),
  datasets: () => import("@/routes/datasets"),
  guided: () => import("@/routes/guided"),
  integrations: () => import("@/routes/integrations"),
  members: () => import("@/routes/members"),
  models: () => import("@/routes/models"),
  runDetail: () => import("@/routes/run-detail"),
  runs: () => import("@/routes/runs"),
  score: () => import("@/routes/score"),
  settings: () => import("@/routes/settings"),
  specEditor: () => import("@/routes/spec-editor"),
  tokens: () => import("@/routes/tokens"),
  usage: () => import("@/routes/usage"),
  // M3.5: workflows (the canvas loads with the editor) and governance.
  workflows: () => import("@/routes/workflows"),
  governance: () => import("@/routes/governance"),
};

export interface RouterContext {
  queryClient: QueryClient;
}

function positiveInt(v: unknown): number | undefined {
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d+$/.test(v) ? Number(v) : NaN;
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[]): T | undefined {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : undefined;
}

function idParam(v: unknown): string | undefined {
  return typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(v) ? v : undefined;
}

function NotFound() {
  return (
    <EmptyState
      title="This page does not exist"
      action={
        <Link to="/" className="text-primary hover:underline">
          Go to Home
        </Link>
      }
    >
      The link may be out of date, or the resource was removed.
    </EmptyState>
  );
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: AppShell,
  notFoundComponent: NotFound,
});

const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: HomePage });

const datasetsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/datasets", component: lazyRouteComponent(page.datasets, "DatasetsPage") });

const datasetRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/datasets/$datasetId",
  component: lazyRouteComponent(page.datasets, "DatasetDetailPage"),
  validateSearch: (s: Record<string, unknown>): { version?: number | undefined; upload?: boolean | undefined } => ({
    version: positiveInt(s.version),
    upload: s.upload === true || s.upload === "true" ? true : undefined,
  }),
});

const buildRoute = createRoute({ getParentRoute: () => rootRoute, path: "/build", component: lazyRouteComponent(page.build, "BuildPage") });

const newSpecRoute = createRoute({ getParentRoute: () => rootRoute, path: "/build/new", component: lazyRouteComponent(page.specEditor, "NewSpecPage") });

const specRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/build/$specId",
  component: lazyRouteComponent(page.specEditor, "SpecEditorPage"),
  validateSearch: (s: Record<string, unknown>): { rev?: number | undefined } => ({ rev: positiveInt(s.rev) }),
});

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: lazyRouteComponent(page.runs, "RunsPage"),
  validateSearch: (s: Record<string, unknown>): { state?: RunState | undefined; kind?: RunKind | undefined } => ({
    state: oneOf(s.state, RUN_STATES),
    kind: oneOf(s.kind, RUN_KINDS),
  }),
});

const runRoute = createRoute({ getParentRoute: () => rootRoute, path: "/runs/$runId", component: lazyRouteComponent(page.runDetail, "RunDetailPage") });

const modelsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/models", component: lazyRouteComponent(page.models, "ModelsPage") });

const modelRoute = createRoute({ getParentRoute: () => rootRoute, path: "/models/$modelId", component: lazyRouteComponent(page.models, "ModelPage") });

const INSPECT_TABS = ["diagnostics", "plots", "tree", "preprocess", "provenance"] as const;

const modelVersionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/models/$modelId/versions/$version",
  component: lazyRouteComponent(page.models, "ModelVersionPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: (typeof INSPECT_TABS)[number] | undefined } => ({
    tab: oneOf(s.tab, INSPECT_TABS),
  }),
});

const scoreRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/score",
  component: lazyRouteComponent(page.score, "ScorePage"),
  validateSearch: (s: Record<string, unknown>): { model?: string | undefined; version?: number | undefined } => ({
    model: idParam(s.model),
    version: positiveInt(s.version),
  }),
});

const catalogRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/catalog",
  component: lazyRouteComponent(page.catalog, "CatalogPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: "steps" | "plots" | undefined } => ({
    tab: oneOf(s.tab, ["steps", "plots"] as const),
  }),
});

const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: lazyRouteComponent(page.settings, "SettingsPage") });

const guidedRoute = createRoute({ getParentRoute: () => rootRoute, path: "/guided", component: lazyRouteComponent(page.guided, "GuidedPage") });

// Guided sessions (M3): a new one starts from a template; a saved one is a spec, one URL per step.
const guidedNewRoute = createRoute({ getParentRoute: () => rootRoute, path: "/guided/new/$templateId", component: lazyRouteComponent(page.guided, "GuidedNewPage") });

const guidedResumeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/guided/$specId", component: lazyRouteComponent(page.guided, "GuidedResumePage") });

const guidedStepRoute = createRoute({ getParentRoute: () => rootRoute, path: "/guided/$specId/$step", component: lazyRouteComponent(page.guided, "GuidedSessionPage") });

const tokensRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tokens",
  component: lazyRouteComponent(page.tokens, "TokensPage"),
  validateSearch: (
    s: Record<string, unknown>,
  ): { all?: boolean | undefined; status?: "active" | "expired" | "revoked" | undefined; preset?: string | undefined } => ({
    all: s.all === true || s.all === "true" ? true : undefined,
    status: oneOf(s.status, ["active", "expired", "revoked"] as const),
    // "score:<alias>": open the New token dialog for one endpoint (from Integrations).
    preset: typeof s.preset === "string" && /^score:[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$/.test(s.preset) ? s.preset : undefined,
  }),
});

export const INTEGRATION_TABS = ["overview", "endpoints", "webhooks", "sharing"] as const;
export type IntegrationTab = (typeof INTEGRATION_TABS)[number];
export const ENDPOINT_TABS = ["overview", "schema", "snippets"] as const;
export type EndpointTab = (typeof ENDPOINT_TABS)[number];

const integrationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/integrations",
  component: lazyRouteComponent(page.integrations, "IntegrationsPage"),
  validateSearch: (
    s: Record<string, unknown>,
  ): { tab?: IntegrationTab | undefined; model?: string | undefined; version?: number | undefined; plot?: string | undefined } => ({
    tab: oneOf(s.tab, INTEGRATION_TABS),
    // A model plot to save and share (from the plot gallery's "Share").
    model: idParam(s.model),
    version: positiveInt(s.version),
    plot: typeof s.plot === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(s.plot) ? s.plot : undefined,
  }),
});

const endpointRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/integrations/endpoints/$alias",
  component: lazyRouteComponent(page.integrations, "EndpointPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: EndpointTab | undefined } => ({ tab: oneOf(s.tab, ENDPOINT_TABS) }),
});

const webhookRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/integrations/webhooks/$webhookId",
  component: lazyRouteComponent(page.integrations, "WebhookPage"),
});

const membersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/members",
  component: lazyRouteComponent(page.members, "MembersPage"),
  validateSearch: (s: Record<string, unknown>): { scope?: "tenant" | undefined } => ({ scope: oneOf(s.scope, ["tenant"] as const) }),
});

const usageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/usage",
  component: lazyRouteComponent(page.usage, "UsagePage"),
  validateSearch: (
    s: Record<string, unknown>,
  ): { period?: "day" | "month" | undefined; start?: string | undefined; scope?: "tenant" | undefined } => ({
    period: oneOf(s.period, ["day", "month"] as const),
    start: typeof s.start === "string" && /^\d{4}-\d{2}(?:-\d{2})?$/.test(s.start) ? s.start : undefined,
    scope: oneOf(s.scope, ["tenant"] as const),
  }),
});

export const ADMIN_TABS = ["tenants", "signups", "quotas", "kill-switches", "cost", "executors", "audit", "promotions"] as const;
export type AdminTab = (typeof ADMIN_TABS)[number];

const adminRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/admin",
  component: lazyRouteComponent(page.admin, "AdminPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: AdminTab | undefined } => ({ tab: oneOf(s.tab, ADMIN_TABS) }),
});

// ----------------------------------------------------------------------------- M3.5

export const WORKFLOW_TABS = ["editor", "runs", "automation", "revisions"] as const;
export type WorkflowTab = (typeof WORKFLOW_TABS)[number];

function templateParam(v: unknown): string | undefined {
  return typeof v === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(v) ? v : undefined;
}

const workflowsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows",
  component: lazyRouteComponent(page.workflows, "WorkflowsPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: "workflows" | "templates" | undefined } => ({ tab: oneOf(s.tab, ["workflows", "templates"] as const) }),
});

const newWorkflowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/new",
  component: lazyRouteComponent(page.workflows, "NewWorkflowPage"),
  validateSearch: (s: Record<string, unknown>): { template?: string | undefined } => ({ template: templateParam(s.template) }),
});

const recipeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/recipe",
  component: lazyRouteComponent(page.workflows, "RecipePage"),
  validateSearch: (s: Record<string, unknown>): { template?: string | undefined } => ({ template: templateParam(s.template) }),
});

const workflowRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/$workflowId",
  component: lazyRouteComponent(page.workflows, "WorkflowPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: WorkflowTab | undefined; rev?: number | undefined } => ({
    tab: oneOf(s.tab, WORKFLOW_TABS),
    rev: positiveInt(s.rev),
  }),
});

const workflowRunRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflow-runs/$runId",
  component: lazyRouteComponent(page.workflows, "WorkflowRunPage"),
});

export const GOVERNANCE_TABS = ["policies", "tenant", "checks"] as const;
export type GovernanceTab = (typeof GOVERNANCE_TABS)[number];
export const VALIDATION_STATUSES = ["draft", "checks_running", "checks_passed", "checks_failed", "awaiting_approval", "validated", "rejected", "deprecated"] as const;

const governanceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/governance",
  component: lazyRouteComponent(page.governance, "GovernancePage"),
  validateSearch: (s: Record<string, unknown>): { tab?: GovernanceTab | undefined } => ({ tab: oneOf(s.tab, GOVERNANCE_TABS) }),
});

const newPolicyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/governance/policies/new",
  component: lazyRouteComponent(page.governance, "NewPolicyPage"),
  validateSearch: (s: Record<string, unknown>): { scope?: "tenant" | undefined } => ({ scope: oneOf(s.scope, ["tenant"] as const) }),
});

const policyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/governance/policies/$policyId",
  component: lazyRouteComponent(page.governance, "PolicyPage"),
  validateSearch: (s: Record<string, unknown>): { scope?: "tenant" | undefined } => ({ scope: oneOf(s.scope, ["tenant"] as const) }),
});

const validationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/validations",
  component: lazyRouteComponent(page.governance, "ValidationsPage"),
  validateSearch: (s: Record<string, unknown>): { status?: (typeof VALIDATION_STATUSES)[number] | undefined } => ({ status: oneOf(s.status, VALIDATION_STATUSES) }),
});

const validationRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/validations/$validationId",
  component: lazyRouteComponent(page.governance, "ValidationPage"),
});

const modelValidationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/models/$modelId/versions/$version/validations",
  component: lazyRouteComponent(page.governance, "ModelValidationsPage"),
});

export const routeTree = rootRoute.addChildren([
  homeRoute,
  datasetsRoute,
  datasetRoute,
  buildRoute,
  newSpecRoute,
  specRoute,
  runsRoute,
  runRoute,
  modelsRoute,
  modelRoute,
  modelVersionRoute,
  scoreRoute,
  catalogRoute,
  settingsRoute,
  guidedRoute,
  guidedNewRoute,
  guidedResumeRoute,
  guidedStepRoute,
  tokensRoute,
  integrationsRoute,
  endpointRoute,
  webhookRoute,
  membersRoute,
  usageRoute,
  adminRoute,
  workflowsRoute,
  newWorkflowRoute,
  recipeRoute,
  workflowRoute,
  workflowRunRoute,
  governanceRoute,
  newPolicyRoute,
  policyRoute,
  validationsRoute,
  validationRoute,
  modelValidationsRoute,
]);

export function createAppRouter(queryClient: QueryClient) {
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: "intent",
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
