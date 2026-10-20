import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, createRoute, createRouter, lazyRouteComponent, Link } from "@tanstack/react-router";

import { EmptyState } from "@/components/common";
import { AppShell } from "@/components/shell/app-shell";
import { isNodePath } from "@/features/datasets/tree";
import { validateRunsSearch, type RunsSearch } from "@/features/runs/run-search";
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
  choose: () => import("@/routes/choose"),
  datasets: () => import("@/routes/datasets"),
  experiments: () => import("@/routes/experiments"),
  exploreData: () => import("@/routes/explore-data"),
  exploreModel: () => import("@/routes/explore-model"),
  guided: () => import("@/routes/guided"),
  integrations: () => import("@/routes/integrations"),
  members: () => import("@/routes/members"),
  models: () => import("@/routes/models"),
  optimize: () => import("@/routes/optimize"),
  runCompare: () => import("@/routes/run-compare"),
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

/** A catalog id (`ts.sarimax`, `core.resample`). */
function catalogId(v: unknown): string | undefined {
  return typeof v === "string" && v.length <= 128 && /^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$/.test(v) ? v : undefined;
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

const newSpecRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/build/new",
  component: lazyRouteComponent(page.specEditor, "NewSpecPage"),
  // M3.5: "Start with this model" (Help me choose, the catalog).
  validateSearch: (s: Record<string, unknown>): { model?: string | undefined } => ({ model: catalogId(s.model) }),
});

const specRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/build/$specId",
  component: lazyRouteComponent(page.specEditor, "SpecEditorPage"),
  validateSearch: (s: Record<string, unknown>): { rev?: number | undefined } => ({ rev: positiveInt(s.rev) }),
});

// v0.14: the runs workspace keeps its filters, columns, order and saved view in the URL.
const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: lazyRouteComponent(page.runs, "RunsPage"),
  validateSearch: (s: Record<string, unknown>): RunsSearch => validateRunsSearch(s),
});

/** Metric (or media) keys (`mapi_spec.tracking.METRIC_KEY_RE`). An empty list is kept: the
 * user cleared every key (no list at all means the default keys). */
function metricKeys(v: unknown, max: number): string[] | undefined {
  if (Array.isArray(v) && v.length === 0) return [];
  const list = Array.isArray(v) ? v.filter((k): k is string => typeof k === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(k)) : [];
  return list.length ? [...new Set(list)].slice(0, max) : undefined;
}

export interface RunCompareSearch {
  runs: string[];
  keys?: string[] | undefined;
  media?: string[] | undefined;
}

// v0.14: compare 2..10 runs. A static segment, so it wins over `/runs/$runId`.
const runCompareRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/compare",
  component: lazyRouteComponent(page.runCompare, "RunComparePage"),
  validateSearch: (s: Record<string, unknown>): RunCompareSearch => ({
    runs: Array.isArray(s.runs) ? [...new Set(s.runs.map(idParam).filter((r): r is string => Boolean(r)))].slice(0, 10) : [],
    keys: metricKeys(s.keys, 12),
    media: metricKeys(s.media, 4),
  }),
});

/** Tabs of the run page (Overview is the default, without `?tab=`): v0.11, v0.14 media. */
export const RUN_TABS = ["metrics", "params", "media", "logs", "notes"] as const;
export type RunTab = (typeof RUN_TABS)[number];

const runRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$runId",
  component: lazyRouteComponent(page.runDetail, "RunDetailPage"),
  validateSearch: (s: Record<string, unknown>): { tab?: RunTab | undefined } => ({ tab: oneOf(s.tab, RUN_TABS) }),
});

// v0.14: experiments (named groups of runs) and one experiment with its runs.
const experimentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/experiments",
  component: lazyRouteComponent(page.experiments, "ExperimentsPage"),
  validateSearch: (s: Record<string, unknown>): { archived?: boolean | undefined } => ({ archived: s.archived === true || s.archived === "true" ? true : undefined }),
});

const experimentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/experiments/$experimentId",
  component: lazyRouteComponent(page.experiments, "ExperimentPage"),
  // the runs table of the page, without the experiment (the page fixes it)
  validateSearch: (s: Record<string, unknown>): RunsSearch => ({ ...validateRunsSearch(s), experiment: undefined }),
});

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

export const CATALOG_TABS = ["models", "steps", "plots"] as const;

const catalogRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/catalog",
  component: lazyRouteComponent(page.catalog, "CatalogPage"),
  // M3.5: without a tab the page browses About cards; `about=kind:id` opens one card.
  validateSearch: (s: Record<string, unknown>): { tab?: (typeof CATALOG_TABS)[number] | undefined; about?: string | undefined } => ({
    tab: oneOf(s.tab, CATALOG_TABS),
    about: typeof s.about === "string" && /^[a-z_]{1,32}:[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$/.test(s.about) ? s.about : undefined,
  }),
});

// M3.5: the explorers, model comparison, budget optimisation and Help me choose.
// v0.10: + the eight sections for scientific data (a hand copy of `features/explore/views.ts DATA_SECTIONS`).
export const DATA_EXPLORER_TABS = ["overview", "distributions", "timeseries", "missingness", "outliers", "correlation", "ccf", "acf_pacf", "stationarity", "stl", "vif", "groups", "quality", "structure", "images", "gallery", "profiles", "map", "network", "tree", "circular", "points"] as const;
export const MODEL_EXPLORER_TABS = ["summary", "params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf", "fevd", "fields", "forecast", "backtest", "governance", "lineage"] as const;

const exploreDataRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/datasets/$datasetId/versions/$version/explore",
  component: lazyRouteComponent(page.exploreData, "DataExplorerPage"),
  // M4: `node` picks the node of a tree dataset.
  validateSearch: (s: Record<string, unknown>): { section?: (typeof DATA_EXPLORER_TABS)[number] | undefined; run?: string | undefined; node?: string | undefined } => ({
    section: oneOf(s.section, DATA_EXPLORER_TABS),
    run: idParam(s.run),
    node: typeof s.node === "string" && isNodePath(s.node) ? s.node : undefined,
  }),
});

const exploreModelRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/models/$modelId/versions/$version/explore",
  component: lazyRouteComponent(page.exploreModel, "ModelExplorerPage"),
  validateSearch: (s: Record<string, unknown>): { section?: (typeof MODEL_EXPLORER_TABS)[number] | undefined; run?: string | undefined } => ({
    section: oneOf(s.section, MODEL_EXPLORER_TABS),
    run: idParam(s.run),
  }),
});

const compareRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/models/compare",
  component: lazyRouteComponent(page.exploreModel, "ComparePage"),
  validateSearch: (s: Record<string, unknown>): { refs?: string[] | undefined; run?: string | undefined } => ({
    refs: Array.isArray(s.refs) ? s.refs.filter((r): r is string => typeof r === "string" && /^model:\/\/\S{1,500}$/.test(r)).slice(0, 11) : undefined,
    run: idParam(s.run),
  }),
});

const optimizeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/models/$modelId/versions/$version/optimize",
  component: lazyRouteComponent(page.optimize, "OptimizePage"),
  validateSearch: (s: Record<string, unknown>): { run?: string | undefined; back?: string | undefined } => ({
    run: idParam(s.run),
    // The Guided analysis (spec id) the page was opened from.
    back: idParam(s.back),
  }),
});

const chooseRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/choose",
  component: lazyRouteComponent(page.choose, "ChoosePage"),
  // Answers as `?goal=forecast,explain_drivers&data_shape=panel` (the server checks them).
  validateSearch: (s: Record<string, unknown>): Record<string, string> =>
    Object.fromEntries(
      Object.entries(s).filter((e): e is [string, string] => /^[a-z][a-z0-9_]{0,31}$/.test(e[0]) && typeof e[1] === "string" && /^[a-z0-9_,]{1,300}$/.test(e[1])),
    ),
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

/** A dataset reference to bind a template's data to (`dataset://project/name@vN|@latest`). */
function datasetRefParam(v: unknown): string | undefined {
  return typeof v === "string" && v.length <= 300 && /^dataset:\/\/[^@\s]+@(latest|v[1-9]\d*)$/.test(v) ? v : undefined;
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
  validateSearch: (s: Record<string, unknown>): { template?: string | undefined; data?: string | undefined } => ({ template: templateParam(s.template), data: datasetRefParam(s.data) }),
});

const recipeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workflows/recipe",
  component: lazyRouteComponent(page.workflows, "RecipePage"),
  validateSearch: (s: Record<string, unknown>): { template?: string | undefined; data?: string | undefined } => ({ template: templateParam(s.template), data: datasetRefParam(s.data) }),
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
  runCompareRoute,
  runRoute,
  experimentsRoute,
  experimentRoute,
  modelsRoute,
  modelRoute,
  modelVersionRoute,
  scoreRoute,
  catalogRoute,
  exploreDataRoute,
  exploreModelRoute,
  compareRoute,
  optimizeRoute,
  chooseRoute,
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
