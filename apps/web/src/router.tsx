import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, createRoute, createRouter, Link } from "@tanstack/react-router";

import type { RunKind, RunState } from "@/api/types";
import { EmptyState } from "@/components/common";
import { AppShell } from "@/components/shell/app-shell";
import { RUN_STATES } from "@/lib/run-state";
import { BuildPage } from "@/routes/build";
import { CatalogPage } from "@/routes/catalog";
import { DatasetDetailPage, DatasetsPage } from "@/routes/datasets";
import { GuidedPage } from "@/routes/guided";
import { HomePage } from "@/routes/home";
import { ModelPage, ModelsPage, ModelVersionPage } from "@/routes/models";
import { RunDetailPage } from "@/routes/run-detail";
import { RUN_KINDS, RunsPage } from "@/routes/runs";
import { ScorePage } from "@/routes/score";
import { SettingsPage } from "@/routes/settings";
import { NewSpecPage, SpecEditorPage } from "@/routes/spec-editor";

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

const datasetsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/datasets", component: DatasetsPage });

const datasetRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/datasets/$datasetId",
  component: DatasetDetailPage,
  validateSearch: (s: Record<string, unknown>): { version?: number | undefined; upload?: boolean | undefined } => ({
    version: positiveInt(s.version),
    upload: s.upload === true || s.upload === "true" ? true : undefined,
  }),
});

const buildRoute = createRoute({ getParentRoute: () => rootRoute, path: "/build", component: BuildPage });

const newSpecRoute = createRoute({ getParentRoute: () => rootRoute, path: "/build/new", component: NewSpecPage });

const specRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/build/$specId",
  component: SpecEditorPage,
  validateSearch: (s: Record<string, unknown>): { rev?: number | undefined } => ({ rev: positiveInt(s.rev) }),
});

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: RunsPage,
  validateSearch: (s: Record<string, unknown>): { state?: RunState | undefined; kind?: RunKind | undefined } => ({
    state: oneOf(s.state, RUN_STATES),
    kind: oneOf(s.kind, RUN_KINDS),
  }),
});

const runRoute = createRoute({ getParentRoute: () => rootRoute, path: "/runs/$runId", component: RunDetailPage });

const modelsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/models", component: ModelsPage });

const modelRoute = createRoute({ getParentRoute: () => rootRoute, path: "/models/$modelId", component: ModelPage });

const INSPECT_TABS = ["diagnostics", "plots", "tree", "preprocess", "provenance"] as const;

const modelVersionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/models/$modelId/versions/$version",
  component: ModelVersionPage,
  validateSearch: (s: Record<string, unknown>): { tab?: (typeof INSPECT_TABS)[number] | undefined } => ({
    tab: oneOf(s.tab, INSPECT_TABS),
  }),
});

const scoreRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/score",
  component: ScorePage,
  validateSearch: (s: Record<string, unknown>): { model?: string | undefined; version?: number | undefined } => ({
    model: idParam(s.model),
    version: positiveInt(s.version),
  }),
});

const catalogRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/catalog",
  component: CatalogPage,
  validateSearch: (s: Record<string, unknown>): { tab?: "steps" | "plots" | undefined } => ({
    tab: oneOf(s.tab, ["steps", "plots"] as const),
  }),
});

const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsPage });

const guidedRoute = createRoute({ getParentRoute: () => rootRoute, path: "/guided", component: GuidedPage });

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
