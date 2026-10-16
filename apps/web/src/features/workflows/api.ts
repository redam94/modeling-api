/**
 * TanStack Query hooks for workflows (M3.5): the node catalog and gallery, documents and
 * revisions, static validation, runs, node runs, output links, schedules and triggers. Keys of
 * project-scoped data start with the project key (`qk.project`), like every other page.
 */
import { keepPreviousData, queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, unwrap, unwrapVoid, unwrapWithStatus } from "@/api/client";
import { qk, useProjectScope, type ProjectScope } from "@/api/queries";

import type { Catalog } from "./ports";
import type {
  AboutKind,
  ScheduleCreate,
  ScheduleUpdate,
  TriggerCreate,
  WorkflowCreate,
  WorkflowRevisionCreate,
  WorkflowRun,
  WorkflowRunCreate,
  WorkflowRunStatus,
} from "./types";

const LIMIT = 200;

export const wk = {
  nodes: ["catalog", "workflow-nodes"] as const,
  templates: ["catalog", "workflow-templates"] as const,
  template: (id: string) => ["catalog", "workflow-templates", id] as const,
  abouts: (kind: AboutKind | null) => ["catalog", "abouts", kind ?? "all"] as const,
  workflows: (p: string) => [...qk.project(p), "workflows"] as const,
  workflow: (p: string, id: string) => [...qk.project(p), "workflows", id] as const,
  revisions: (p: string, id: string) => [...qk.project(p), "workflows", id, "revisions"] as const,
  revision: (p: string, id: string, r: number) => [...qk.project(p), "workflows", id, "revisions", r] as const,
  runs: (p: string, id: string) => [...qk.project(p), "workflows", id, "runs"] as const,
  schedules: (p: string, id: string) => [...qk.project(p), "workflows", id, "schedules"] as const,
  triggers: (p: string, id: string) => [...qk.project(p), "workflows", id, "triggers"] as const,
  run: (p: string, id: string) => [...qk.project(p), "workflow-runs", id] as const,
  nodeRun: (p: string, id: string, node: string) => [...qk.project(p), "workflow-runs", id, "nodes", node] as const,
};

export const TERMINAL_RUN: ReadonlySet<WorkflowRunStatus> = new Set(["succeeded", "failed", "cancelled"]);

// ------------------------------------------------------------------------- catalog

export const nodeCatalogQuery = queryOptions({
  queryKey: wk.nodes,
  queryFn: ({ signal }) => unwrap(api.GET("/v1/catalog/workflow-nodes", { params: { query: { limit: LIMIT } }, signal })),
  staleTime: 10 * 60_000,
});

/** The node catalog as a map by implementation id. */
export function useNodeCatalog() {
  const q = useQuery(nodeCatalogQuery);
  const map: Catalog = useMemo(() => new Map((q.data?.items ?? []).map((n) => [n.id, n])), [q.data]);
  return { ...q, map };
}

export function useWorkflowTemplates() {
  return useQuery({
    queryKey: wk.templates,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/workflow-templates", { params: { query: { limit: LIMIT } }, signal })),
    staleTime: 10 * 60_000,
  });
}

export function useWorkflowTemplate(id: string | undefined) {
  return useQuery({
    queryKey: wk.template(id ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/workflow-templates/{template_id}", { params: { path: { template_id: id ?? "" } }, signal })),
    enabled: Boolean(id),
    staleTime: 10 * 60_000,
  });
}

/** About cards of one kind of catalog item (`GET /v1/catalog/abouts?kind=`). */
export function useAbouts(kind: AboutKind | null, enabled = true) {
  return useQuery({
    queryKey: wk.abouts(kind),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/catalog/abouts", { params: { query: { limit: LIMIT, ...(kind ? { kind } : {}) } }, signal })),
    staleTime: 10 * 60_000,
    enabled,
  });
}

// ------------------------------------------------------------------------- workflows

export function useWorkflows(archived = false) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...wk.workflows(s.key), { archived }],
    queryFn: ({ signal }) => unwrap(api.GET("/v1/workflows", { params: { query: { limit: LIMIT, archived } }, headers: s.headers, signal })),
  });
}

export function useWorkflow(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: wk.workflow(s.key, id ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/workflows/{workflow_id}", { params: { path: { workflow_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

export function useWorkflowRevisions(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: wk.revisions(s.key, id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/workflows/{workflow_id}/revisions", {
          params: { path: { workflow_id: id ?? "" }, query: { limit: LIMIT } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id),
  });
}

export const revisionQuery = (s: ProjectScope, id: string, revision: number) =>
  queryOptions({
    queryKey: wk.revision(s.key, id, revision),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/workflows/{workflow_id}/revisions/{revision}", {
          params: { path: { workflow_id: id, revision } },
          headers: s.headers,
          signal,
        }),
      ),
    staleTime: Infinity, // revisions are immutable
  });

export function useWorkflowRevision(id: string | undefined, revision: number | undefined) {
  const s = useProjectScope();
  return useQuery({ ...revisionQuery(s, id ?? "", revision ?? 0), enabled: Boolean(id) && Boolean(revision) });
}

/** Static validation (stores nothing). Never evaluated in the browser: the server parses conditions. */
export function validateWorkflow(scope: ProjectScope, document: Record<string, unknown>, signal?: AbortSignal) {
  return unwrap(api.POST("/v1/workflows:validate", { body: { document: document as never }, headers: scope.headers, signal }));
}

export function useCreateWorkflow() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: WorkflowCreate) => unwrap(api.POST("/v1/workflows", { body, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: wk.workflows(s.key) }),
  });
}

export function useCreateWorkflowRevision(workflowId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: WorkflowRevisionCreate) =>
      unwrap(api.POST("/v1/workflows/{workflow_id}/revisions", { params: { path: { workflow_id: workflowId } }, headers: s.headers, body })),
    onSuccess: async (rev) => {
      qc.setQueryData(wk.revision(s.key, workflowId, rev.revision), rev);
      await Promise.all([
        qc.invalidateQueries({ queryKey: wk.workflow(s.key, workflowId), exact: true }),
        qc.invalidateQueries({ queryKey: wk.revisions(s.key, workflowId) }),
        qc.invalidateQueries({ queryKey: wk.workflows(s.key), exact: false }),
      ]);
    },
  });
}

export function useUpdateWorkflow(workflowId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: { description?: string | null; archived?: boolean | null }) =>
      unwrap(api.PATCH("/v1/workflows/{workflow_id}", { params: { path: { workflow_id: workflowId } }, headers: s.headers, body })),
    onSuccess: (wf) => {
      qc.setQueryData(wk.workflow(s.key, workflowId), wf);
      void qc.invalidateQueries({ queryKey: wk.workflows(s.key) });
    },
  });
}

export function useDeleteWorkflow() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (workflowId: string) => unwrapVoid(api.DELETE("/v1/workflows/{workflow_id}", { params: { path: { workflow_id: workflowId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: wk.workflows(s.key) }),
  });
}

// ------------------------------------------------------------------------- runs

export function useWorkflowRuns(workflowId: string | undefined, status?: WorkflowRunStatus) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...wk.runs(s.key, workflowId ?? ""), status ?? null],
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/workflows/{workflow_id}/runs", {
          params: { path: { workflow_id: workflowId ?? "" }, query: { limit: 50, ...(status ? { status } : {}) } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(workflowId),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.items.some((r) => !TERMINAL_RUN.has(r.status)) ? 5000 : false),
  });
}

export function useStartWorkflowRun(workflowId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ body, key }: { body: WorkflowRunCreate; key: string }) =>
      unwrapWithStatus(
        api.POST("/v1/workflows/{workflow_id}/runs", {
          params: { path: { workflow_id: workflowId }, header: { "Idempotency-Key": key } },
          headers: s.headers,
          body,
        }),
      ),
    onSuccess: ({ data }) => {
      qc.setQueryData(wk.run(s.key, data.run_id), data);
      void qc.invalidateQueries({ queryKey: wk.runs(s.key, workflowId) });
    },
  });
}

export function useWorkflowRun(runId: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: wk.run(s.key, runId ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/workflow-runs/{run_id}", { params: { path: { run_id: runId ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(runId),
    // SSE drives live updates; this is a slow safety net while the run is active.
    refetchInterval: (q) => (q.state.data && !TERMINAL_RUN.has(q.state.data.status) ? 15_000 : false),
  });
}

export function useCancelWorkflowRun() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (runId: string) => unwrap(api.POST("/v1/workflow-runs/{run_id}:cancel", { params: { path: { run_id: runId } }, headers: s.headers })),
    onSuccess: (run: WorkflowRun) => {
      qc.setQueryData(wk.run(s.key, run.run_id), run);
      void qc.invalidateQueries({ queryKey: wk.runs(s.key, run.workflow_id) });
    },
  });
}

/** A signed link to a node's file output (a derived dataset, a large table). */
export function nodeOutputLink(scope: ProjectScope, runId: string, nodeId: string, port: string) {
  return unwrap(
    api.GET("/v1/workflow-runs/{run_id}/nodes/{node_id}/outputs/{port}", {
      params: { path: { run_id: runId, node_id: nodeId, port } },
      headers: scope.headers,
    }),
  );
}

// ------------------------------------------------------------------------- automation

export function useSchedules(workflowId: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: wk.schedules(s.key, workflowId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/workflows/{workflow_id}/schedules", { params: { path: { workflow_id: workflowId }, query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

export function useScheduleMutations(workflowId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  const invalidate = () => qc.invalidateQueries({ queryKey: wk.schedules(s.key, workflowId) });
  const path = { workflow_id: workflowId };
  return {
    create: useMutation({
      mutationFn: (body: ScheduleCreate) => unwrap(api.POST("/v1/workflows/{workflow_id}/schedules", { params: { path }, headers: s.headers, body })),
      onSuccess: invalidate,
    }),
    update: useMutation({
      mutationFn: ({ id, body }: { id: string; body: ScheduleUpdate }) =>
        unwrap(api.PATCH("/v1/workflows/{workflow_id}/schedules/{schedule_id}", { params: { path: { ...path, schedule_id: id } }, headers: s.headers, body })),
      onSuccess: invalidate,
    }),
    remove: useMutation({
      mutationFn: (id: string) =>
        unwrapVoid(api.DELETE("/v1/workflows/{workflow_id}/schedules/{schedule_id}", { params: { path: { ...path, schedule_id: id } }, headers: s.headers })),
      onSuccess: invalidate,
    }),
  };
}

export function useTriggers(workflowId: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: wk.triggers(s.key, workflowId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/workflows/{workflow_id}/triggers", { params: { path: { workflow_id: workflowId }, query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

export function useTriggerMutations(workflowId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  const invalidate = () => qc.invalidateQueries({ queryKey: wk.triggers(s.key, workflowId) });
  const path = { workflow_id: workflowId };
  return {
    create: useMutation({
      mutationFn: (body: TriggerCreate) => unwrap(api.POST("/v1/workflows/{workflow_id}/triggers", { params: { path }, headers: s.headers, body })),
      onSuccess: invalidate,
    }),
    remove: useMutation({
      mutationFn: (id: string) =>
        unwrapVoid(api.DELETE("/v1/workflows/{workflow_id}/triggers/{trigger_id}", { params: { path: { ...path, trigger_id: id } }, headers: s.headers })),
      onSuccess: invalidate,
    }),
  };
}
